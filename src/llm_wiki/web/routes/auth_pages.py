"""Login/logout pages, OIDC SSO start/callback, and the public share view."""
from __future__ import annotations

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ...markdown_render import render_markdown
from ...services import audit
from ...services import oidc as oidc_svc
from ...services.auth import (
    MAX_USERNAME_LEN,
    authenticate,
    create_session,
    delete_session,
    resolve_or_provision_oidc_user,
)
from ...services.errors import NotFoundError, ValidationError
from ...util import normalize_client_ip
from .deps import WebDeps


def _safe_return_path(raw: str | None) -> str:
    """Allow only same-site relative paths (leading single slash, not //)."""
    if not raw:
        return "/"
    path = raw.strip()
    if not path.startswith("/") or path.startswith("//"):
        return "/"
    # Reject backslash tricks and scheme-relative edge cases.
    if "\\" in path or "://" in path:
        return "/"
    return path


def register_auth_pages(web: FastAPI, deps: WebDeps) -> None:
    db = deps.db
    docs = deps.docs
    secret = deps.secret
    user = deps.user
    render = deps.render
    login_redirect = deps.login_redirect
    login_limiter = deps.login_limiter
    settings = deps.app.settings

    def _login_ctx(**extra):
        return {"oidc_enabled": bool(settings.oidc_enabled), **extra}

    @web.get("/login", response_class=HTMLResponse)
    def login_get(request: Request):
        if user(request):
            return RedirectResponse("/", status_code=303)
        return render("login.html", request, error=None, **_login_ctx())

    @web.post("/login", response_class=HTMLResponse)
    def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
        ip = normalize_client_ip(request.client.host if request.client else None)
        uname = (username or "").strip().lower()[:MAX_USERNAME_LEN]
        # Throttle by client IP only. A per-username counter would let anyone lock a
        # known account (e.g. admin) out from its own clean IP just by spamming bad
        # passwords — the lockout itself becomes the DoS. Failed attempts are still
        # audit-logged with the username so a spray is detectable.
        ip_key = f"ip:{ip}"
        if not login_limiter.allowed(ip_key):
            return render(
                "login.html",
                request,
                status=429,
                error="Too many attempts. Please wait a few minutes and try again.",
                **_login_ctx(),
            )
        p = authenticate(db, username, password)
        if not p:
            just_blocked = login_limiter.record_failure(ip_key)
            audit.record_tx(db, actor=uname or "-", via="web", action="login_failed",
                            target=None, outcome="error", detail=f"ip={ip}")
            if just_blocked:
                # The failure that just crossed the throttle threshold — record one
                # block event (subsequent attempts short-circuit at the allowed() gate
                # above without re-auditing) so a brute-force surfaces in the admin feed
                # instead of leaving only a wall of identical login_failed rows.
                audit.record_tx(db, actor=uname or "-", via="web", action="login_blocked",
                                target=None, outcome="blocked", detail=f"ip={ip}")
            return render(
                "login.html",
                request,
                status=401,
                error="Invalid username or password.",
                **_login_ctx(),
            )
        # Keep the IP-wide failure history until the sliding window expires.  If any
        # successful login cleared it, an attacker with one valid low-privilege account
        # could alternate guesses against another account with successful logins of
        # their own and bypass the brute-force limit indefinitely.
        # Drop any pre-login session state (fixation hardening), then bind the new
        # session. A fresh CSRF token is minted on the next rendered page.
        ret = _safe_return_path(request.query_params.get("next"))
        request.session.clear()
        request.session["sid"] = create_session(
            db,
            p,
            audit_actor=p.username,
            audit_via="web",
            audit_detail=f"ip={ip}",
        )
        return RedirectResponse(ret, status_code=303)

    @web.post("/logout")
    def logout(request: Request):
        delete_session(db, request.session.get("sid"))
        request.session.clear()
        return login_redirect()

    # ---- OIDC SSO (authorization-code + PKCE) ----------------------------
    @web.get("/auth/oidc/login")
    def oidc_login(request: Request):
        if not settings.oidc_enabled:
            return render(
                "login.html",
                request,
                status=404,
                error="SSO is not enabled on this server.",
                **_login_ctx(),
            )
        if user(request):
            return RedirectResponse("/", status_code=303)
        try:
            meta = oidc_svc.fetch_discovery(settings.oidc_issuer)
            auth_ep = meta.get("authorization_endpoint")
            if not auth_ep:
                raise ValidationError("OIDC discovery missing authorization_endpoint")
            verifier, challenge = oidc_svc.generate_pkce_pair()
            state = oidc_svc.generate_state()
            nonce = oidc_svc.generate_nonce()
            ret = _safe_return_path(request.query_params.get("next"))
            # Persist PKCE + anti-CSRF state in the session until the callback.
            request.session["oidc_state"] = state
            request.session["oidc_nonce"] = nonce
            request.session["oidc_code_verifier"] = verifier
            request.session["oidc_return"] = ret
            url = oidc_svc.build_authorization_url(
                authorization_endpoint=str(auth_ep),
                client_id=settings.oidc_client_id,
                redirect_uri=settings.oidc_redirect_uri,
                scope=settings.oidc_scopes,
                state=state,
                nonce=nonce,
                code_challenge=challenge,
            )
            return RedirectResponse(url, status_code=302)
        except ValidationError as e:
            return render(
                "login.html",
                request,
                status=502,
                error=f"SSO is temporarily unavailable: {e.message}",
                **_login_ctx(),
            )

    @web.get("/auth/oidc/callback", response_class=HTMLResponse)
    def oidc_callback(request: Request):
        if not settings.oidc_enabled:
            return render(
                "login.html",
                request,
                status=404,
                error="SSO is not enabled on this server.",
                **_login_ctx(),
            )

        ip = normalize_client_ip(request.client.host if request.client else None)
        ip_key = f"ip:{ip}"

        def _fail(message: str, *, status: int = 401) -> HTMLResponse:
            just_blocked = login_limiter.record_failure(ip_key)
            audit.record_tx(
                db,
                actor="-",
                via="web",
                action="login_failed",
                target=None,
                outcome="error",
                detail=f"ip={ip} via=oidc",
            )
            if just_blocked:
                audit.record_tx(
                    db,
                    actor="-",
                    via="web",
                    action="login_blocked",
                    target=None,
                    outcome="blocked",
                    detail=f"ip={ip} via=oidc",
                )
            # Drop half-finished OIDC session material on failure.
            for key in (
                "oidc_state",
                "oidc_nonce",
                "oidc_code_verifier",
                "oidc_return",
            ):
                request.session.pop(key, None)
            return render(
                "login.html",
                request,
                status=status,
                error=message,
                **_login_ctx(),
            )

        if not login_limiter.allowed(ip_key):
            return render(
                "login.html",
                request,
                status=429,
                error="Too many attempts. Please wait a few minutes and try again.",
                **_login_ctx(),
            )

        err = request.query_params.get("error")
        if err:
            desc = request.query_params.get("error_description") or err
            return _fail(f"SSO login was denied: {desc}")

        code = request.query_params.get("code")
        state = request.query_params.get("state")
        expected_state = request.session.get("oidc_state")
        nonce = request.session.get("oidc_nonce")
        verifier = request.session.get("oidc_code_verifier")
        ret = _safe_return_path(request.session.get("oidc_return"))

        if not code or not state or not expected_state or state != expected_state:
            return _fail("Invalid SSO state. Please try signing in again.")
        if not nonce or not verifier:
            return _fail("SSO session expired. Please try signing in again.")

        try:
            meta = oidc_svc.fetch_discovery(settings.oidc_issuer)
            token_ep = meta.get("token_endpoint")
            jwks_uri = meta.get("jwks_uri")
            if not token_ep or not jwks_uri:
                raise ValidationError("OIDC discovery missing token_endpoint or jwks_uri")
            tokens = oidc_svc.exchange_code_for_tokens(
                token_endpoint=str(token_ep),
                code=code,
                redirect_uri=settings.oidc_redirect_uri,
                client_id=settings.oidc_client_id,
                client_secret=settings.oidc_client_secret,
                code_verifier=verifier,
            )
            id_token = tokens.get("id_token")
            if not id_token or not isinstance(id_token, str):
                raise ValidationError("token response missing id_token")
            claims = oidc_svc.verify_id_token(
                id_token,
                issuer=settings.oidc_issuer,
                client_id=settings.oidc_client_id,
                jwks_uri=str(jwks_uri),
                nonce=nonce,
            )
            if settings.oidc_require_email_verified and claims.email:
                if claims.email_verified is False:
                    raise ValidationError("email address is not verified at the identity provider")
            preferred = oidc_svc.claim_username(claims, settings.oidc_username_claim)
            principal = resolve_or_provision_oidc_user(
                db,
                issuer=claims.issuer,
                sub=claims.subject,
                email=claims.email,
                preferred_username=preferred,
                settings=settings,
            )
        except ValidationError as e:
            return _fail(e.message)
        except Exception:
            return _fail("SSO login failed. Please try again.")

        # Success: clear fixation surface (including OIDC material), mint session.
        request.session.clear()
        request.session["sid"] = create_session(
            db,
            principal,
            audit_actor=principal.username,
            audit_via="web",
            audit_detail=f"ip={ip} via=oidc",
        )
        return RedirectResponse(ret, status_code=303)

    @web.get("/share/{token}", response_class=HTMLResponse)
    def public_share(token: str, request: Request):
        """Unauthenticated read-only view of a single document via signed token."""
        from ...services import share as share_svc

        try:
            path = share_svc.verify_share_token(secret, token, db=db)
            doc = docs.get(path)
        except ValidationError as e:
            return render("error.html", request, status=400, message=e.message)
        except NotFoundError as e:
            return render("error.html", request, status=404, message=e.message)
        # A share token authorizes exactly this one document.  Authenticated document
        # views expand ![[transclusions]], but doing that here would disclose the full
        # bodies of other notes to an anonymous token holder.  Without a resolver the
        # renderer keeps embeds as ordinary wikilinks, preserving the single-path grant.
        html = render_markdown(doc["content"], doc["path"])
        return render("share.html", request, status=200, doc=doc, html=html)
