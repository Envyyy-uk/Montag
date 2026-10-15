"""FastAPI-застосунок."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from core.config import Settings, get_settings
from core.db import open_db, schema_version
from core.system_check import run_all
from server import media, projects, uploads


def create_app(settings: Settings | None = None, start_worker: bool | None = None) -> FastAPI:
    settings = settings or get_settings()
    if start_worker is None:
        start_worker = settings.run_worker_in_server

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.ensure_dirs()
        open_db(settings.database_path).close()
        worker = None
        if start_worker:
            from worker.runner import Worker
            worker = Worker(settings)
            worker.start()
        yield
        if worker:
            worker.stop()

    app = FastAPI(title="Автомонтаж", lifespan=lifespan)
    app.state.settings = settings
    app.include_router(projects.router)
    app.include_router(uploads.router)
    app.include_router(media.router)

    @app.get("/api/health")
    def health() -> dict:
        conn = open_db(settings.database_path)
        try:
            version = schema_version(conn)
        finally:
            conn.close()
        return {"status": "ok", "schema_version": version}

    @app.get("/api/system")
    def system() -> dict:
        return run_all().as_dict()

    @app.get("/api/queue")
    def queue_status() -> list[dict]:
        conn = open_db(settings.database_path)
        try:
            rows = conn.execute(
                "SELECT j.id, j.kind, j.status, j.progress, j.error, j.project_id, p.name AS project"
                " FROM jobs j LEFT JOIN projects p ON p.id = j.project_id"
                " WHERE j.status != 'done' OR j.finished_at > strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-1 day')"
                " ORDER BY j.id DESC LIMIT 200").fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    # Сервер слухає лише на 127.0.0.1; Tailscale-інтерфейс додається в Спринті 12.
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
