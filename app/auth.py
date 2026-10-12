"""Optionale Anmeldung: ein gemeinsames Passwort (`access_password`), Sitzung per signiertem Cookie.

Leeres Passwort = keine Anmeldung (Standard, wie bisher). Offen bleiben immer: /api/health, /api/version (Watchdog-Prüfung),
/api/auth/*, die Seitenhülle und statische Dateien (enthalten keine Daten). Alles andere unter /api und der WebSocket braucht die Sitzung.
Schutz vor Raten: nach 5 Fehlversuchen innerhalb einer Minute sperrt die Anmeldung für eine Minute (global, da hinter
`tailscale serve` alle Anfragen von 127.0.0.1 kommen). Das Cookie ist HttpOnly, SameSite=Strict und bei https „Secure“.
Das Passwort steht im Klartext in config.toml (Datei nicht weitergeben); es gibt keine Benutzerverwaltung.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Callable

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

COOKIE = "mc_session"
SESSION_S = 30 * 86400
OPEN_API = ("/api/health", "/api/version", "/api/peer/summary", "/api/maintenance", "/api/sends")     # Stationsabfrage prüft ihr eigenes Token
MAX_FAILS, FAIL_WINDOW_S, LOCK_S = 5, 60.0, 60.0


class Auth:
    def __init__(self, password: str, clock: Callable[[], float] = time.time):
        self.password = password or ""
        self._clock = clock
        self._key = hashlib.sha256(("mc-session:" + self.password).encode()).digest()
        self._fails: list[float] = []
        self._locked_until = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.password)

    def _sign(self, exp: int) -> str:
        return f"{exp}.{hmac.new(self._key, str(exp).encode(), hashlib.sha256).hexdigest()}"

    def make_token(self) -> str:
        return self._sign(int(self._clock()) + SESSION_S)

    def valid(self, token: str | None) -> bool:
        if not self.enabled:
            return True
        try:
            exp_s, _ = (token or "").split(".", 1)
            return int(exp_s) > self._clock() and hmac.compare_digest(token, self._sign(int(exp_s)))
        except (ValueError, TypeError):
            return False

    def allowed(self, path: str) -> bool:
        return not path.startswith("/api/") and path != "/ws" or path in OPEN_API or path.startswith("/api/auth/")

    def locked(self) -> float:
        return max(0.0, self._locked_until - self._clock())

    def check_password(self, given: str) -> bool:
        """Prüft das Passwort mit Fehlversuchszähler. Gesperrt → immer False."""
        now = self._clock()
        if self.locked() > 0:
            return False
        ok = hmac.compare_digest(hashlib.sha256(given.encode()).digest(), hashlib.sha256(self.password.encode()).digest())
        if ok:
            self._fails.clear()
            return True
        self._fails = [t for t in self._fails if now - t < FAIL_WINDOW_S] + [now]
        if len(self._fails) >= MAX_FAILS:
            self._locked_until, self._fails = now + LOCK_S, []
        return False


class Login(BaseModel):
    password: str


def make_router(auth: Auth) -> APIRouter:
    r = APIRouter(prefix="/api/auth")

    @r.get("/status")
    async def status(request: Request) -> dict:
        return {"required": auth.enabled, "logged_in": auth.valid(request.cookies.get(COOKIE))}

    @r.post("/login")
    async def login(body: Login, request: Request, response: Response):
        if not auth.enabled:
            return {"ok": True}
        wait = auth.locked()
        if wait > 0:
            return JSONResponse({"detail": f"Zu viele Fehlversuche. Bitte in {int(wait) + 1} s erneut versuchen."}, status_code=429)
        if not auth.check_password(body.password[:200]):
            left = auth.locked()
            msg = "Zu viele Fehlversuche. Eine Minute Pause." if left > 0 else "Falsches Passwort."
            return JSONResponse({"detail": msg}, status_code=429 if left > 0 else 401)
        secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
        response.set_cookie(COOKIE, auth.make_token(), max_age=SESSION_S, httponly=True, samesite="strict", secure=secure)
        return {"ok": True}

    @r.post("/logout")
    async def logout(response: Response) -> dict:
        response.delete_cookie(COOKIE)
        return {"ok": True}

    return r
