"""FastAPI-App: REST + WebSocket + statische Oberfläche für einen MeshCore-Node."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from . import auth as authmod
from .api import make_router
from .config import load_settings
from .node import NodeError
from .runtime import Runtime, build
from .supervisor import Supervisor

log = logging.getLogger("meshcore-remote")
STATIC = Path(__file__).parent / "static"


class NoCacheStaticFiles(StaticFiles):
    """Ohne expliziten Header cachen Browser (besonders als installierte App auf dem Handy) oft stunden-
    lang, ohne überhaupt nachzufragen – ein Update kommt dann erst nach manuellem Leeren des Caches an.
    `no-cache` erzwingt eine Rückfrage beim Server bei jedem Laden (ETag/Last-Modified sorgen weiter für
    schnelle 304-Antworten, wenn sich nichts geändert hat)."""
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(rt: Runtime) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        logging.basicConfig(level=logging.DEBUG if rt.settings.debug else logging.INFO)
        # Jeder Startschritt einzeln geschützt: die Weboberfläche soll auch dann laufen, wenn die Automatik
        # nicht startet. Der Grund steht dann als Hinweis in der Oberfläche.
        try:
            await rt.automation.start()
        except Exception as e:
            log.exception("Automatik konnte nicht starten")
            rt.state["problems"].append(f"Automatik nicht gestartet: {type(e).__name__}: {e}")
        task = asyncio.create_task(Supervisor(rt).run())
        stats_task = asyncio.create_task(rt.stats.run()) if rt.stats else None
        room_task = asyncio.create_task(rt.room.run()) if rt.room else None
        peer_task = asyncio.create_task(rt.peers.run()) if rt.peers else None
        yield
        task.cancel()
        if peer_task:
            peer_task.cancel()
        if room_task:
            room_task.cancel()
        if stats_task:
            stats_task.cancel()
        for step in (rt.automation.stop, rt.node.disconnect):
            try:
                await step()
            except Exception:
                log.exception("Fehler beim Beenden")
        try:
            rt.raw_store.close()
        except Exception:
            pass

    app = FastAPI(title="MeshCore Remote", version=__version__, lifespan=lifespan)
    auth = authmod.Auth(rt.settings.access_password)
    app.state.auth = auth
    app.include_router(authmod.make_router(auth))
    app.include_router(make_router(rt))

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        if auth.enabled and not auth.allowed(request.url.path) and not auth.valid(request.cookies.get(authmod.COOKIE)):
            return JSONResponse({"detail": "Nicht angemeldet."}, status_code=401)
        return await call_next(request)

    @app.exception_handler(sqlite3.Error)
    async def db_error(_, exc: sqlite3.Error) -> JSONResponse:
        log.exception("Datenbankfehler", exc_info=exc)
        return JSONResponse({"detail": f"Datenbank-Problem: {exc}. Das Programm läuft weiter."}, status_code=503)

    @app.exception_handler(Exception)
    async def unexpected_error(_, exc: Exception) -> JSONResponse:
        log.exception("Unerwarteter Fehler in der Anfrage", exc_info=exc)
        return JSONResponse({"detail": f"Interner Fehler: {type(exc).__name__}: {exc}"}, status_code=500)

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        if auth.enabled and not auth.valid(ws.cookies.get(authmod.COOKIE)):
            await ws.close(code=1008)
            return
        await ws.accept()
        rt.hub.clients.add(ws)
        try:
            await ws.send_json({"kind": "history", "events": list(rt.hub.history)})
            while True:
                # Eingehende Nachrichten (z. B. Ping) ignorieren, Verbindung offen halten
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception:
            log.debug("WebSocket beendet", exc_info=True)    # kaputte Verbindung: still schließen
        finally:
            rt.hub.clients.discard(ws)

    @app.get("/")
    async def index():
        page = STATIC / "index.html"
        if page.is_file():
            return FileResponse(page, headers={"Cache-Control": "no-cache"})
        return HTMLResponse("<h1>MeshCore Remote</h1><p>Die Oberfläche (app/static/index.html) fehlt. "
                            "Die Schnittstelle läuft: <a href='/api/status'>/api/status</a></p>")

    # Installierbare Web-App (Startbildschirm) und Push: Service Worker muss unter „/“ liegen, damit sein Geltungsbereich passt
    @app.get("/sw.js", include_in_schema=False)
    async def service_worker():
        return FileResponse(STATIC / "sw.js", media_type="text/javascript",
                            headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})

    @app.get("/manifest.webmanifest", include_in_schema=False)
    async def manifest():
        return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json",
                            headers={"Cache-Control": "no-cache"})

    if STATIC.is_dir():
        app.mount("/static", NoCacheStaticFiles(directory=STATIC), name="static")
    return app


def create_default_app() -> FastAPI:
    """App mit Konfiguration aus config.toml / MC_*-Variablen (für uvicorn --factory)."""
    return create_app(build(load_settings()))


def run() -> None:
    import uvicorn

    settings = load_settings()
    uvicorn.run("app.main:create_default_app", factory=True,
                host=settings.host, port=settings.port)


if __name__ == "__main__":
    run()
