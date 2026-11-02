"""FastAPI: API рейтинга + раздача фронта.

Запуск локально:  uvicorn app.main:app --reload   (из папки backend/)
"""
import logging
import os
import secrets
from datetime import timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import apify_source as src
from .collector import RefreshRefused, execute_run, running_run, start_run
from .config import Settings
from .db import Run, import_creators, make_session_factory
from .ranking import build_ranking, creator_videos, last_good_run, save_totals, to_local_iso

logging.basicConfig(level=logging.INFO)


def create_app(settings: Optional[Settings] = None, client_factory=src.get_client) -> FastAPI:
    settings = settings or Settings.from_env()
    session_factory = make_session_factory(settings.database_url)
    tz = ZoneInfo(settings.tz)

    with session_factory() as s:
        if os.path.exists(settings.creators_file):
            n = import_creators(s, settings.creators_file)
            logging.info("Загружено блогеров из списка: %s", n)
        else:
            logging.warning("Файл со списком блогеров не найден: %s", settings.creators_file)

    app = FastAPI(title="BXD Battle", docs_url=None, redoc_url=None, openapi_url=None)

    def no_store(payload):
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/api/ranking")
    def ranking():
        with session_factory() as s:
            return no_store(build_ranking(s, settings))

    @app.get("/api/creators/{creator_id}/videos")
    def videos(creator_id: int):
        with session_factory() as s:
            data = creator_videos(s, creator_id, tz)
        if data is None:
            raise HTTPException(404, "Участник не найден")
        return no_store({"videos": data})

    @app.get("/api/status")
    def status():
        with session_factory() as s:
            last = s.query(Run).order_by(Run.id.desc()).first()
            good = last_good_run(s)
            nxt = None
            if good:
                nxt = to_local_iso(good.finished_at + timedelta(hours=settings.min_refresh_hours), tz)
            return no_store({
                "running": running_run(s) is not None,
                "last_run": None if not last else {
                    "status": last.status, "message": last.message, "cost_usd": last.cost_usd,
                    "started_at": to_local_iso(last.started_at, tz),
                    "finished_at": to_local_iso(last.finished_at, tz) if last.finished_at else None},
                "next_refresh_allowed_at": nxt,
            })

    @app.post("/api/refresh", status_code=202)
    def refresh(background: BackgroundTasks, force: bool = False,
                x_admin_token: Optional[str] = Header(default=None)):
        """Запускает платный сбор. Доступно только с заголовком X-Admin-Token."""
        if not settings.admin_token:
            raise HTTPException(503, "Обновление отключено: не задан ADMIN_TOKEN")
        if not x_admin_token or not secrets.compare_digest(x_admin_token, settings.admin_token):
            raise HTTPException(401, "Неверный токен")
        if not settings.apify_token:
            raise HTTPException(503, "Не задан APIFY_TOKEN")
        try:
            run_id = start_run(session_factory, settings, force=force)
        except RefreshRefused as e:
            raise HTTPException(e.code, e.message)
        def job():
            execute_run(run_id, session_factory, settings, client_factory)
            save_totals(session_factory, settings, run_id)
        background.add_task(job)
        return {"run_id": run_id, "message": "Сбор запущен"}

    # Фронт отдаём последним, чтобы он не перекрывал /api
    if os.path.isdir(settings.frontend_dir):
        app.mount("/", StaticFiles(directory=settings.frontend_dir, html=True), name="front")

    return app


app = create_app() if os.environ.get("BXD_NO_AUTOSTART") != "1" else None
