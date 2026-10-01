from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from rebuildx_presence.engine import PresenceConfig, PresenceEngine, PresenceEvent, Visibility

WEB_DIR = Path(__file__).resolve().parents[3] / "frontend"

DEMO_USERS = [
    ("you", "You (Hari)", {"alice", "bob", "chen", "diya"}),
    ("alice", "Alice", {"you", "bob", "chen"}),
    ("bob", "Bob", {"you", "alice", "diya"}),
    ("chen", "Chen", {"you", "alice"}),
    ("diya", "Diya", {"you", "bob"}),
]


class ConnectionHub:
    def __init__(self, engine: PresenceEngine) -> None:
        self.engine = engine
        self.sockets: dict[str, WebSocket] = {}
        self.user_sessions: dict[str, set[str]] = {}

    def register(self, session_id: str, user_id: str, ws: WebSocket) -> None:
        self.sockets[session_id] = ws
        self.user_sessions.setdefault(user_id, set()).add(session_id)

    def drop(self, session_id: str, user_id: str | None) -> None:
        self.sockets.pop(session_id, None)
        if user_id and user_id in self.user_sessions:
            self.user_sessions[user_id].discard(session_id)
            if not self.user_sessions[user_id]:
                self.user_sessions.pop(user_id, None)

    async def send_user(self, user_id: str, payload: dict[str, Any]) -> None:
        for sid in list(self.user_sessions.get(user_id, set())):
            ws = self.sockets.get(sid)
            if ws is None:
                continue
            try:
                await ws.send_json(payload)
            except Exception:
                self.drop(sid, user_id)

    async def fanout(self, events: list[PresenceEvent]) -> None:
        for event in events:
            for viewer_id, view in self.engine.recipients_for(event):
                await self.send_user(
                    viewer_id,
                    {
                        "type": "presence",
                        "userId": view.user_id,
                        "displayName": view.display_name,
                        "online": view.online,
                        "lastSeenAt": view.last_seen_at,
                        "hidden": view.hidden,
                        "reason": event.reason,
                    },
                )

    async def close_expired_sockets(self) -> None:
        live = set(self.engine.sessions)
        for session_id, ws in list(self.sockets.items()):
            if session_id in live:
                continue
            try:
                await ws.close()
            except Exception:
                pass


def create_app() -> FastAPI:
    engine = PresenceEngine(clock=time.time, config=PresenceConfig())
    for user_id, name, contacts in DEMO_USERS:
        engine.upsert_user(user_id, name, contacts)

    hub = ConnectionHub(engine)
    app = FastAPI(title="ReBuildX Presence")
    app.state.engine = engine
    app.state.hub = hub

    @app.on_event("startup")
    async def _startup() -> None:
        app.state.sweep_task = asyncio.create_task(_sweep_loop(app))

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        task = getattr(app.state, "sweep_task", None)
        if task:
            task.cancel()

    @app.get("/api/config")
    def config() -> dict[str, Any]:
        c = engine.config
        return {
            "sessionTtlSeconds": c.session_ttl_seconds,
            "heartbeatHintSeconds": c.heartbeat_hint_seconds,
            "offlineDebounceSeconds": c.offline_debounce_seconds,
            "users": [
                {"id": u.user_id, "name": u.display_name, "visibility": u.visibility.value}
                for u in engine.users.values()
            ],
        }

    @app.get("/api/metrics")
    def metrics() -> dict[str, Any]:
        return {
            **engine.metrics,
            "activeSessions": len(engine.sessions),
            "onlineUsers": sum(1 for u in engine.users.values() if u.session_ids),
        }

    @app.post("/api/users/{user_id}/visibility")
    def set_visibility(user_id: str, body: dict[str, str]) -> dict[str, str]:
        engine.set_visibility(user_id, Visibility(body["visibility"]))
        return {"ok": "true"}

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        session_id = f"{id(ws)}-{time.time_ns()}"
        user_id: str | None = None
        try:
            while True:
                msg = await ws.receive_json()
                mtype = msg.get("type")
                if mtype == "auth":
                    user_id = str(msg["userId"])
                    events = engine.connect(user_id, session_id)
                    hub.register(session_id, user_id, ws)
                    contact_ids = sorted(engine.users[user_id].contact_ids)
                    engine.subscribe(user_id, contact_ids)
                    snapshot = [
                        {
                            "userId": v.user_id,
                            "displayName": v.display_name,
                            "online": v.online,
                            "lastSeenAt": v.last_seen_at,
                            "hidden": v.hidden,
                        }
                        for v in engine.snapshot(user_id, contact_ids)
                    ]
                    await ws.send_json(
                        {
                            "type": "hello",
                            "sessionId": session_id,
                            "userId": user_id,
                            "contacts": snapshot,
                        }
                    )
                    await hub.fanout(events)
                elif mtype == "heartbeat":
                    engine.heartbeat(session_id)
                elif mtype == "visibility":
                    if user_id:
                        engine.set_visibility(user_id, Visibility(msg["visibility"]))
                elif mtype == "chat":
                    if user_id:
                        receiver_id = msg.get("to")
                        text = msg.get("text")
                        if receiver_id and text:
                            chat_msg = {
                                "type": "chat",
                                "from": user_id,
                                "to": receiver_id,
                                "text": text,
                                "timestamp": time.time(),
                            }
                            await hub.send_user(receiver_id, chat_msg)
                            await hub.send_user(user_id, chat_msg)
                else:
                    await ws.send_json({"type": "error", "message": f"unknown type {mtype}"})
        except WebSocketDisconnect:
            pass
        finally:
            events = engine.disconnect(session_id)
            hub.drop(session_id, user_id)
            if user_id and not engine.is_online(user_id):
                engine.unsubscribe_all(user_id)
            await hub.fanout(events)

    if WEB_DIR.exists():
        app.mount("/assets", StaticFiles(directory=WEB_DIR), name="assets")

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(WEB_DIR / "index.html")

    return app


async def _sweep_loop(app: FastAPI) -> None:
    engine: PresenceEngine = app.state.engine
    hub: ConnectionHub = app.state.hub
    interval = engine.config.sweep_interval_seconds
    while True:
        await asyncio.sleep(interval)
        events = engine.sweep()
        if events:
            await hub.fanout(events)
        await hub.close_expired_sockets()


app = create_app()
