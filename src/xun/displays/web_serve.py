"""Temporary static file servers for directories inside an agent's workdir.

Serves one directory, or the workdir root itself, as a website under
``/srv/{key}/`` until it expires. Plain static hosting without the file
API's per-response hardening: served files share the application's origin,
so servers are opt-in and short-lived.
"""

from __future__ import annotations

import asyncio
import html
import os
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import anyio
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.datastructures import URL
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Mount
from starlette.types import Scope

from .web_file import AgentGetter, resolve_path

SERVE_TTL_SECONDS = 3600
MAX_SERVE_SERVERS = 16


class _ListingFiles(StaticFiles):
    """Serve files, and list a directory when it has no index.html."""

    async def get_response(self, path: str, scope: Scope):
        full_path, stat_result = await anyio.to_thread.run_sync(self.lookup_path, path)
        if stat_result and stat.S_ISDIR(stat_result.st_mode):
            index_path, index_stat = await anyio.to_thread.run_sync(self.lookup_path, os.path.join(path, "index.html"))
            if index_stat is not None and stat.S_ISREG(index_stat.st_mode):
                return await super().get_response(path, scope)
            if not scope["path"].endswith("/"):
                url = URL(scope=scope)
                return RedirectResponse(url.replace(path=url.path + "/"))
            page = await anyio.to_thread.run_sync(self._index_html, path, full_path)
            return HTMLResponse(page)
        return await super().get_response(path, scope)

    @staticmethod
    def _index_html(path: str, full_path: str) -> str:
        entries = []
        for name in os.listdir(full_path):
            child = os.path.join(full_path, name)
            try:
                is_dir = os.path.isdir(child)
            except OSError:
                continue
            entries.append((not is_dir, name.lower(), name, is_dir))
        entries.sort()
        items = []
        if path not in ("", "."):
            items.append('<li><a href="../">../</a></li>')
        for _dir_last, _sort_name, name, is_dir in entries:
            href = quote(name) + ("/" if is_dir else "")
            label = html.escape(name + ("/" if is_dir else ""), quote=True)
            items.append(f'<li><a href="{html.escape(href, quote=True)}">{label}</a></li>')
        title = "/" if path in ("", ".") else "/" + html.escape(path, quote=True)
        body = "\n".join(items) or "<li>空目录</li>"
        return (
            "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            f"<title>{title}</title></head><body><h1>{title}</h1><ul>{body}</ul></body></html>"
        )


class ServeRequest(BaseModel):
    path: str = ""


@dataclass
class _ServeSession:
    path: str
    target: Path
    mount: Mount
    expires_at: float
    timer: asyncio.TimerHandle


class ServeManager:
    """Keeps temporary ``StaticFiles`` mounts alive until they expire or stop.

    All methods run on the event loop; ``close`` is the only cross-thread
    entry point and only cancels timers there.
    """

    def __init__(
        self,
        app: FastAPI,
        *,
        ttl: float = SERVE_TTL_SECONDS,
        max_servers: int = MAX_SERVE_SERVERS,
    ) -> None:
        self._app = app
        self._ttl = ttl
        self._max_servers = max_servers
        self._sessions: dict[str, _ServeSession] = {}

    def start(self, path: str, target: Path) -> dict[str, Any]:
        self._sweep()
        if len(self._sessions) >= self._max_servers:
            raise HTTPException(429, "Too many active servers; stop one first")
        key = secrets.token_urlsafe(12)
        mount = Mount(f"/srv/{key}", _ListingFiles(directory=target, html=True, follow_symlink=False))
        self._app.routes.append(mount)
        session = _ServeSession(
            path=path,
            target=target,
            mount=mount,
            expires_at=time.time() + self._ttl,
            timer=asyncio.get_running_loop().call_later(self._ttl, self.stop, key),
        )
        self._sessions[key] = session
        return self._info(key, session)

    def stop(self, key: str) -> bool:
        session = self._sessions.pop(key, None)
        if session is None:
            return False
        session.timer.cancel()
        self._remove_mount(session.mount)
        return True

    def list(self) -> list[dict[str, Any]]:
        self._sweep()
        return [self._info(key, session) for key, session in self._sessions.items()]

    def close(self) -> None:
        for session in self._sessions.values():
            session.timer.cancel()
            self._remove_mount(session.mount)
        self._sessions.clear()

    def _remove_mount(self, mount: Mount) -> None:
        try:
            self._app.routes.remove(mount)
        except ValueError:
            pass  # the display app was torn down already

    def _sweep(self) -> None:
        # a served directory removed leaves a dead mount behind; retire it.
        now = time.time()
        dead = [key for key, s in self._sessions.items() if s.expires_at <= now or not s.target.is_dir()]
        for key in dead:
            self.stop(key)

    @staticmethod
    def _info(key: str, session: _ServeSession) -> dict[str, Any]:
        return {"key": key, "path": session.path, "url": f"/srv/{key}/", "expires_at": session.expires_at}


def build_serve_router(agent_getter: AgentGetter, manager: ServeManager) -> APIRouter:
    """Build the serve API router; mounts live on the manager's display app."""
    router = APIRouter()

    @router.get("/api/serve/{agent_id}")
    async def list_servers(agent_id: str) -> dict[str, list[dict[str, Any]]]:
        agent_getter(agent_id)
        return {"servers": manager.list()}

    @router.post("/api/serve/{agent_id}/start")
    async def start_server(agent_id: str, request: ServeRequest) -> dict[str, Any]:
        agent = agent_getter(agent_id)
        # an empty path resolves to the workdir root, which is servable too
        target = resolve_path(agent, request.path)
        if not target.is_dir():
            raise HTTPException(404, "Directory not found")
        return manager.start(request.path, target)

    @router.post("/api/serve/{agent_id}/{key}/stop")
    async def stop_server(agent_id: str, key: str) -> dict[str, bool]:
        agent_getter(agent_id)
        if not manager.stop(key):
            raise HTTPException(404, "Server not found")
        return {"stopped": True}

    return router
