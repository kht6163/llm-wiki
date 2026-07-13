"""Login/logout pages and the public share view."""
from __future__ import annotations

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ...markdown_render import render_markdown
from ...services import audit
from ...services.auth import (
    MAX_USERNAME_LEN,
    authenticate,
    create_session,
    delete_session,
)
from ...services.errors import NotFoundError, ValidationError
from ...util import normalize_client_ip
from .deps import WebDeps


def register_auth_pages(web: FastAPI, deps: WebDeps) -> None:
    db = deps.db
    docs = deps.docs
    secret = deps.secret
    user = deps.user
    render = deps.render
    login_redirect = deps.login_redirect
    login_limiter = deps.login_limiter

    @web.get("/login", response_class=HTMLResponse)
    def login_get(request: Request):
        if user(request):
            return RedirectResponse("/", status_code=303)
        return render("login.html", request, error=None)

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
            return render("login.html", request, status=429,
                          error="Too many attempts. Please wait a few minutes and try again.")
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
            return render("login.html", request, status=401, error="Invalid username or password.")
        # Keep the IP-wide failure history until the sliding window expires.  If any
        # successful login cleared it, an attacker with one valid low-privilege account
        # could alternate guesses against another account with successful logins of
        # their own and bypass the brute-force limit indefinitely.
        # Drop any pre-login session state (fixation hardening), then bind the new
        # session. A fresh CSRF token is minted on the next rendered page.
        request.session.clear()
        request.session["sid"] = create_session(
            db,
            p,
            audit_actor=p.username,
            audit_via="web",
            audit_detail=f"ip={ip}",
        )
        return RedirectResponse("/", status_code=303)

    @web.post("/logout")
    def logout(request: Request):
        delete_session(db, request.session.get("sid"))
        request.session.clear()
        return login_redirect()

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
