"""FastAPI-застосунок."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from core.config import Settings, get_settings
from core.db import open_db, schema_version
from core.system_check import run_all
from server import auth, budget, edits, mcp_access, media, music, projects, requests, styles, ui, uploads


class RequestRetrier:
    """Окремий потік, щоб правки, які чекають на Gemini, не стояли в черзі за довгими рендерами."""

    def __init__(self, settings: Settings, every: float = 60):
        import threading
        self.settings, self.every, self._stop = settings, every, threading.Event()
        self._thread = threading.Thread(target=self._run, name="request-retry", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=30)

    def _run(self) -> None:
        import logging

        from server.requests import retry_waiting
        while not self._stop.wait(self.every):
            try:
                conn = open_db(self.settings.database_path)
                try:
                    if conn.execute("SELECT 1 FROM edit_requests WHERE parse_state = 'waiting' AND status = 'pending' LIMIT 1").fetchone():
                        retry_waiting(conn, self.settings)
                finally:
                    conn.close()
            except Exception:  # noqa: BLE001 — фон не має падати
                logging.getLogger(__name__).exception("повтор правок")


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
            retrier = RequestRetrier(settings)
            retrier.start()
        yield
        if worker:
            worker.stop()
            retrier.stop()

    from render.looks import register_user_dir
    register_user_dir(settings.user_styles_path)

    app = FastAPI(title="Автомонтаж", lifespan=lifespan)
    app.state.settings = settings
    app.add_middleware(auth.PinMiddleware)
    app.include_router(auth.router)
    app.include_router(projects.router)
    app.include_router(uploads.router)
    app.include_router(media.router)
    app.include_router(budget.router)
    app.include_router(edits.router)
    app.include_router(styles.router)
    app.include_router(requests.router)
    app.include_router(ui.router)
    app.include_router(mcp_access.router)
    app.include_router(music.router)

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

    @app.get("/api/drive")
    def drive_status() -> dict:
        from sources import gdrive
        conn = open_db(settings.database_path)
        try:
            return gdrive.status(conn, settings)
        finally:
            conn.close()

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

    ui.mount_web(app)  # після всіх /api — інакше перехопить їх
    return app


def listeners(settings: Settings) -> list[dict]:
    """127.0.0.1 (HTTP, для самого ноутбука) + Tailscale-адреса (HTTPS, для iPhone). Ніколи не 0.0.0.0."""
    from core import tailscale
    from core.config import _check_bind_host

    out = [{"host": settings.host, "port": settings.port}]
    ts_host = settings.tailscale_host or tailscale.self_ip() or ""
    if ts_host:
        ts_host = _check_bind_host(ts_host)  # лише 100.64.0.0/10
        cert, key = settings.resolve(settings.tls_cert_file), settings.resolve(settings.tls_key_file)
        item = {"host": ts_host, "port": settings.https_port}
        if cert.exists() and key.exists():
            item |= {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}
        out.append(item)
    return out


def main() -> None:
    import asyncio

    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    if not settings.tailscale_host:
        from core import tailscale
        if tailscale.self_ip() is None and tailscale._cli():
            logging.info("чекаю на Tailscale (до 2 хв), щоб iPhone міг підключитись…")
            if tailscale.wait_for_ip() is None:
                logging.warning("Tailscale не запустився — доступ з iPhone буде після перезапуску програми")
    app = create_app(settings)
    servers = []
    for i, ln in enumerate(listeners(settings)):
        # життєвий цикл (воркер) — лише в першого слухача, інакше воркерів було б два
        cfg = uvicorn.Config(app, lifespan="on" if i == 0 else "off", log_level="info", **ln)
        servers.append(uvicorn.Server(cfg))
        scheme = "https" if "ssl_certfile" in ln else "http"
        logging.info("слухаю %s://%s:%s", scheme, ln["host"], ln["port"])
        if i and scheme == "http":
            logging.warning("Tailscale без HTTPS: PWA і push на iPhone не працюватимуть — python -m core tailscale-cert")
        if i and not settings.access_pin.get_secret_value():
            logging.warning("ACCESS_PIN не задано — віддалений доступ закритий")

    if settings.mcp_remote:
        from mcp_server.remote import build_remote_app, public_url
        url = public_url(settings)
        if url:
            # лише 127.0.0.1: назовні його виводить Tailscale Funnel, а не прямий слухач
            mcp_app = build_remote_app(settings, app, url)
            servers.append(uvicorn.Server(uvicorn.Config(mcp_app, host="127.0.0.1", port=settings.mcp_http_port,
                                                         lifespan="on", log_level="info")))
            logging.info("MCP для Claude: %s/mcp → 127.0.0.1:%s", url, settings.mcp_http_port)
        else:
            logging.warning("MCP_REMOTE=true, але адреса невідома: запусти Tailscale з MagicDNS або задай MCP_PUBLIC_URL")

    async def run():
        await asyncio.gather(*(s.serve() for s in servers))
    asyncio.run(run())


if __name__ == "__main__":
    main()
