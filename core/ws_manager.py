from __future__ import annotations
import asyncio, json
from datetime import datetime
from fastapi import WebSocket


class WebSocketManager:

    def __init__(self):
        self.active: list[WebSocket] = []
        self._user_connections: dict[str, list[WebSocket]] = {}
        self._lock = asyncio.Lock()
        self._message_count = 0
        self._connect_count = 0
        self._error_count   = 0

    async def connect(self, ws: WebSocket, account_id: str = "") -> None:
        await ws.accept()
        async with self._lock:
            self.active.append(ws)
            if account_id:
                self._user_connections.setdefault(account_id, []).append(ws)
            self._connect_count += 1

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self.active:
            self.active.remove(ws)
        for conns in self._user_connections.values():
            if ws in conns:
                conns.remove(ws)

    async def send_to(self, ws: WebSocket, event: str, data: dict) -> bool:
        payload = json.dumps({
            "event":     event,
            "data":      data,
            "timestamp": datetime.utcnow().isoformat(),
        })
        try:
            await ws.send_text(payload)
            self._message_count += 1
            return True
        except Exception:
            self.disconnect(ws)
            self._error_count += 1
            return False

    async def broadcast(self, event: str, data: dict) -> int:
        """Send to ALL connected clients — used for ping/keepalive only."""
        if not self.active:
            return 0
        payload = json.dumps({
            "event":     event,
            "data":      data,
            "timestamp": datetime.utcnow().isoformat(),
        })
        dead, sent = [], 0
        for ws in list(self.active):
            try:
                await ws.send_text(payload)
                sent += 1
                self._message_count += 1
            except Exception:
                dead.append(ws)
                self._error_count += 1
        for ws in dead:
            self.disconnect(ws)
        return sent

    async def broadcast_to_user(self, account_id: str, event: str, data: dict) -> int:
        """Send event only to connections belonging to a specific user."""
        conns = self._user_connections.get(account_id, [])
        if not conns:
            return 0
        payload = json.dumps({
            "event":     event,
            "data":      data,
            "timestamp": datetime.utcnow().isoformat(),
        })
        dead, sent = [], 0
        for ws in list(conns):
            try:
                await ws.send_text(payload)
                sent += 1
                self._message_count += 1
            except Exception:
                dead.append(ws)
                self._error_count += 1
        for ws in dead:
            self.disconnect(ws)
        return sent

    async def ping(self) -> int:
        """Send keepalive ping to all clients."""
        return await self.broadcast("ping", {"ts": datetime.utcnow().isoformat()})

    def stats(self) -> dict:
        return {
            "active_connections": len(self.active),
            "total_connects":     self._connect_count,
            "messages_sent":      self._message_count,
            "send_errors":        self._error_count,
        }


ws_manager = WebSocketManager()