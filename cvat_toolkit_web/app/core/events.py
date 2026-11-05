from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict, List, Set

from app.core.config import MAX_LOG_ENTRIES


class SSEManager:
    """Gerenciador centralizado de canais Server-Sent Events (SSE)."""

    def __init__(self) -> None:
        self._channels: Dict[str, Set[asyncio.Queue]] = {
            "training": set(),
            "prediction": set(),
        }
        self._history: Dict[str, List[Dict[str, Any]]] = {
            "training": [],
            "prediction": [],
        }

    def subscribe(self, channel: str) -> asyncio.Queue:
        if channel not in self._channels:
            self._channels[channel] = set()
        queue: asyncio.Queue = asyncio.Queue()
        self._channels[channel].add(queue)
        return queue

    def unsubscribe(self, channel: str, queue: asyncio.Queue) -> None:
        if channel in self._channels:
            self._channels[channel].discard(queue)

    def broadcast(self, channel: str, event_data: Dict[str, Any]) -> None:
        """Envia mensagem para todos os clientes conectados ao canal e armazena histórico recente."""
        if "time" not in event_data:
            event_data["time"] = datetime.now(timezone.utc).strftime("%H:%M:%S")

        # Mantém histórico recente (buffer circular configurável via .env)
        if channel in self._history:
            self._history[channel].append(event_data)
            if len(self._history[channel]) > MAX_LOG_ENTRIES:
                self._history[channel] = self._history[channel][-MAX_LOG_ENTRIES:]


        queues = self._channels.get(channel, set())
        for q in list(queues):
            try:
                q.put_nowait(event_data)
            except Exception:
                pass

    def clear_history(self, channel: str) -> None:
        if channel in self._history:
            self._history[channel].clear()

    def get_history(self, channel: str) -> List[Dict[str, Any]]:
        return list(self._history.get(channel, []))

    async def event_generator(self, channel: str) -> AsyncGenerator[str, None]:
        queue = self.subscribe(channel)
        # Primeiro, envia os eventos recentes de histórico
        for hist_event in self.get_history(channel):
            yield f"data: {json.dumps(hist_event, ensure_ascii=False)}\n\n"

        try:
            while True:
                data = await queue.get()
                yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            self.unsubscribe(channel, queue)


sse_manager = SSEManager()
