"""FastAPI-застосунок. Поки лише службові ендпоінти."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from core.config import Settings, get_settings
from core.db import open_db, schema_version
from core.system_check import run_all


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.ensure_dirs()
        conn = open_db(settings.database_path)
        conn.close()
        yield

    app = FastAPI(title="Автомонтаж", lifespan=lifespan)
    app.state.settings = settings

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

    return app


def main() -> None:
    import uvicorn

    settings = get_settings()
    # Сервер слухає лише на 127.0.0.1; Tailscale-інтерфейс додається в Спринті 12.
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
