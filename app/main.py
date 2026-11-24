"""FastAPI application: JSON API plus the static single page frontend.

Run with:  uvicorn app.main:create_app --factory
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterator

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import service
from .config import Settings, load_settings
from .db import connect, init_db
from .sender import Sender, build_sender
from .service import Actor, DeskError
from .telegram_auth import InitDataError, make_init_data, verify_init_data

STATIC_DIR = Path(__file__).parent / "static"
AUTH_SCHEME = "tma"

# Fake reviewers for local development only. See /api/dev/init-data.
DEV_USERS = {
    1001: ("Dev", "Reviewer One"),
    1002: ("Dev", "Reviewer Two"),
}


class NewDraft(BaseModel):
    recipient_name: str = Field(max_length=80)
    recipient_phone: str = Field(max_length=20)
    body: str = Field(max_length=2000)
    purpose: str = Field(default="", max_length=80)


class EditRequest(BaseModel):
    body: str = Field(max_length=2000)
    base_version: int = Field(ge=1)


class ApproveRequest(BaseModel):
    version: int = Field(ge=1)


class RejectRequest(BaseModel):
    version: int = Field(ge=1)
    reason: str = Field(default="", max_length=1000)


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": {"code": code, "message": message}})


def create_app(settings: Settings | None = None, sender: Sender | None = None) -> FastAPI:
    settings = settings or load_settings()
    sender = sender or build_sender(settings.sms_provider)

    boot = connect(settings.db_path)
    init_db(boot)
    boot.close()

    app = FastAPI(title="SMS Approval Desk", version="1.0.0", docs_url="/api/docs", redoc_url=None)
    app.state.settings = settings
    app.state.sender = sender

    @app.exception_handler(DeskError)
    async def _desk_error(_: Request, exc: DeskError) -> JSONResponse:
        return _error(exc.status_code, exc.code, exc.message)

    def get_conn() -> Iterator[sqlite3.Connection]:
        conn = connect(settings.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def current_actor(authorization: str | None = Header(default=None)) -> Actor:
        """Every API call re-verifies the Telegram signature. There are no sessions."""
        scheme, _, init_data = (authorization or "").partition(" ")
        if scheme.lower() != AUTH_SCHEME or not init_data.strip():
            raise DeskError(401, "missing_init_data", "Open this page from the Telegram bot")
        try:
            user = verify_init_data(
                init_data.strip(), settings.bot_token, settings.init_data_max_age_seconds
            )
        except InitDataError as exc:
            raise DeskError(401, exc.code, exc.message) from exc
        if settings.staff_ids and user.id not in settings.staff_ids:
            raise DeskError(403, "not_staff", "Your Telegram account is not on the staff list")
        return Actor(id=user.id, name=user.display_name)

    # ------------------------------------------------------------ open routes

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "env": settings.app_env, "sender": sender.name}

    @app.get("/api/dev/init-data")
    def dev_init_data(user: int = Query(default=1001)):
        """Sign init data for a fake reviewer so the page works in a plain browser.

        Exists only in dev, where the bot token is the fake one. It still goes
        through the real verification on every later request.
        """
        if not settings.is_dev:
            return _error(404, "not_found", "Not found")
        if user not in DEV_USERS:
            return _error(404, "unknown_dev_user", f"Dev users are {sorted(DEV_USERS)}")
        first, last = DEV_USERS[user]
        return {
            "init_data": make_init_data(settings.bot_token, user, first, last),
            "dev_users": [
                {"id": uid, "name": f"{names[0]} {names[1]}"} for uid, names in DEV_USERS.items()
            ],
        }

    # ------------------------------------------------------- signed-in routes

    @app.get("/api/me")
    def me(actor: Actor = Depends(current_actor), conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        return {
            "id": actor.id,
            "name": actor.name,
            "counts": service.status_counts(conn),
            "require_second_approver": settings.require_second_approver,
            "max_body_chars": settings.max_body_chars,
        }

    @app.get("/api/drafts")
    def list_drafts(
        status: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=500),
        _: Actor = Depends(current_actor),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict:
        return {
            "drafts": service.list_drafts(conn, status, limit),
            "counts": service.status_counts(conn),
        }

    @app.post("/api/drafts", status_code=201)
    def create_draft(
        payload: NewDraft,
        actor: Actor = Depends(current_actor),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict:
        return service.create_draft(
            conn,
            settings,
            payload.recipient_name,
            payload.recipient_phone,
            payload.body,
            payload.purpose,
            source="manual",
            actor=actor,
        )

    @app.get("/api/drafts/{draft_id}")
    def get_draft(
        draft_id: int,
        _: Actor = Depends(current_actor),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict:
        return service.get_draft(conn, draft_id)

    @app.post("/api/drafts/{draft_id}/edit")
    def edit_draft(
        draft_id: int,
        payload: EditRequest,
        actor: Actor = Depends(current_actor),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict:
        return service.edit_draft(conn, settings, draft_id, payload.body, payload.base_version, actor)

    @app.post("/api/drafts/{draft_id}/approve")
    def approve_draft(
        draft_id: int,
        payload: ApproveRequest,
        actor: Actor = Depends(current_actor),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict:
        return service.approve_draft(conn, settings, sender, draft_id, payload.version, actor)

    @app.post("/api/drafts/{draft_id}/reject")
    def reject_draft(
        draft_id: int,
        payload: RejectRequest,
        actor: Actor = Depends(current_actor),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict:
        return service.reject_draft(conn, draft_id, payload.version, payload.reason, actor)

    @app.post("/api/drafts/{draft_id}/retry")
    def retry_draft(
        draft_id: int,
        actor: Actor = Depends(current_actor),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict:
        return service.retry_send(conn, sender, draft_id, actor)

    @app.get("/api/outbox")
    def outbox(
        _: Actor = Depends(current_actor), conn: sqlite3.Connection = Depends(get_conn)
    ) -> dict:
        return {"outbox": service.list_outbox(conn)}

    # --------------------------------------------------------------- frontend

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
