"""Per-user settings (API keys) and admin user-management pages."""
from __future__ import annotations

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ...services import audit
from ...services import users as users_svc
from ...services.auth import (
    MAX_USERNAME_LEN,
    Principal,
    create_api_key,
    create_user,
    list_api_keys,
    revoke_api_key,
)
from ...services.errors import WikiError
from .deps import WebDeps


def register_settings_admin(web: FastAPI, deps: WebDeps) -> None:
    db = deps.db
    docs = deps.docs
    render = deps.render
    require_user = deps.require_user
    require_admin = deps.require_admin
    key_limiter = deps.key_limiter

    def _settings_ctx(p: Principal, **extra):
        return {
            "keys": list_api_keys(db, p.user_id),
            "embedding_status": docs.embedding_status() if p.can_admin else None,
            **extra,
        }

    # ---- settings (per-user API keys) -----------------------------------
    @web.get("/settings", response_class=HTMLResponse)
    def settings_get(request: Request, p: Principal = Depends(require_user)):
        return render("settings.html", request, **_settings_ctx(p, new_key=None))

    @web.post("/settings/keys", response_class=HTMLResponse)
    def settings_create_key(request: Request, name: str = Form("key"),
                            scope: str = Form("readwrite"),
                            p: Principal = Depends(require_user)):
        # Throttle minting per user (a hijacked session shouldn't be able to spray keys).
        key_key = f"user:{p.user_id}"
        if not key_limiter.allowed(key_key):
            return render(
                "settings.html",
                request,
                status=429,
                **_settings_ctx(
                    p,
                    new_key=None,
                    error="키 발급이 너무 잦습니다. 잠시 후 다시 시도하세요.",
                ),
            )
        key_limiter.record_failure(key_key)  # count this mint toward the window
        # Render the freshly-minted key directly in the response instead of
        # round-tripping it through the (signed-but-not-encrypted) session cookie.
        # Let ValidationError/Forbidden bubble to the app WikiError handler so
        # coverage audits (key_change) and structured error headers stay consistent.
        token = create_api_key(
            db,
            p,
            name,
            scope=scope,
            audit_actor=p.username,
            audit_via="web",
        )
        return render("settings.html", request, **_settings_ctx(p, new_key=token))

    @web.post("/settings/keys/{key_id}/revoke")
    def settings_revoke_key(key_id: int, request: Request, p: Principal = Depends(require_user)):
        try:
            revoke_api_key(
                db,
                p,
                key_id,
                audit_actor=p.username,
                audit_via="web",
            )
        except WikiError as e:
            audit.record_tx(
                db,
                actor=p.username,
                via="web",
                action="key_revoke",
                target=str(key_id),
                outcome="error",
                detail=e.message,
            )
            request.session["flash"] = e.message
        return RedirectResponse("/settings", status_code=303)

    # ---- admin (require_admin dependency: 403 for non-admins, redirect if anon) ----
    @web.get("/admin/users", response_class=HTMLResponse)
    def admin_users(request: Request, _p: Principal = Depends(require_admin)):
        return render("admin.html", request, users=users_svc.list_users(db))

    @web.post("/admin/users")
    def admin_create(request: Request, username: str = Form(...), password: str = Form(...),
                     role: str = Form("editor"), p: Principal = Depends(require_admin)):
        try:
            create_user(
                db,
                username,
                password,
                role,
                audit_actor=p.username,
                audit_via="web",
            )
        except WikiError as e:
            audit.record_tx(
                db,
                actor=p.username,
                via="web",
                action="user_create",
                target=(username or "")[:MAX_USERNAME_LEN],
                outcome="error",
                detail=e.message,
            )
            request.session["flash"] = e.message
        return RedirectResponse("/admin/users", status_code=303)

    @web.post("/admin/users/{uid}/role")
    def admin_role(uid: int, request: Request, role: str = Form(...),
                   p: Principal = Depends(require_admin)):
        try:
            users_svc.set_role(
                db,
                uid,
                role,
                audit_actor=p.username,
                audit_via="web",
            )
        except WikiError as e:
            audit.record_tx(db, actor=p.username, via="web", action="role_change",
                            target=str(uid), outcome="error", detail=e.message)
            request.session["flash"] = e.message
        return RedirectResponse("/admin/users", status_code=303)

    @web.post("/admin/users/{uid}/active")
    def admin_active(uid: int, request: Request, active: int = Form(...),
                     p: Principal = Depends(require_admin)):
        try:
            users_svc.set_active(
                db,
                uid,
                bool(active),
                audit_actor=p.username,
                audit_via="web",
            )
        except WikiError as e:
            audit.record_tx(db, actor=p.username, via="web", action="user_active",
                            target=str(uid), outcome="error", detail=e.message)
            request.session["flash"] = e.message
        return RedirectResponse("/admin/users", status_code=303)

    @web.post("/admin/users/{uid}/password")
    def admin_password(uid: int, request: Request, password: str = Form(...),
                       p: Principal = Depends(require_admin)):
        try:
            users_svc.set_password(
                db,
                uid,
                password,
                audit_actor=p.username,
                audit_via="web",
            )
        except WikiError as e:
            audit.record_tx(db, actor=p.username, via="web", action="password_change",
                            target=str(uid), outcome="error", detail=e.message)
            request.session["flash"] = e.message
        return RedirectResponse("/admin/users", status_code=303)

    @web.post("/admin/users/{uid}/delete")
    def admin_delete(uid: int, request: Request, p: Principal = Depends(require_admin)):
        try:
            users_svc.delete_user(
                db,
                uid,
                audit_actor=p.username,
                audit_via="web",
            )
        except WikiError as e:
            audit.record_tx(db, actor=p.username, via="web", action="user_delete",
                            target=str(uid), outcome="error", detail=e.message)
            request.session["flash"] = e.message
        return RedirectResponse("/admin/users", status_code=303)
