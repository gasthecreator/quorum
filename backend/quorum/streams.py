"""Replayable event logs behind the SSE endpoints (runs and jobs)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from pydantic import BaseModel

from .store import Store

RUN_TERMINAL = {"final", "error"}
CHAT_TERMINAL: set[str] = set()  # a chat stream stays open until the client leaves
JOB_TERMINAL = {"job_done", "job_error"}


class EventLog:
    def __init__(self, stream_id: str, store: Store, terminal: set[str],
                 existing: list[dict] | None = None):
        self.stream_id, self.store, self.terminal = stream_id, store, terminal
        self.events: list[dict] = existing or []
        self.done = bool(self.events and self.events[-1]["event"] in terminal)
        self._ev = asyncio.Event()

    def append(self, event: str, data: BaseModel | dict) -> None:
        payload = data.model_dump(mode="json") if isinstance(data, BaseModel) else data
        env = {"seq": len(self.events) + 1, "event": event, "data": payload}
        self.events.append(env)
        self.store.append_event(self.stream_id, env["seq"], event, payload)
        if event in self.terminal:
            self.done = True
        ev, self._ev = self._ev, asyncio.Event()
        ev.set()

    def close(self) -> None:
        self.done = True
        ev, self._ev = self._ev, asyncio.Event()
        ev.set()

    async def follow(self, after: int = 0) -> AsyncIterator[dict]:
        i = max(0, after)
        while True:
            while i < len(self.events):
                yield self.events[i]
                i += 1
            if self.done:
                return
            ev = self._ev
            await ev.wait()


def sse_frame(env: dict) -> str:
    return f"id: {env['seq']}\nevent: {env['event']}\ndata: {json.dumps(env['data'])}\n\n"


async def sse_body(log: EventLog, after: int = 0, heartbeat_s: float = 15.0) -> AsyncIterator[str]:
    """Frames for a StreamingResponse. Clients must close after a terminal event.

    The pending read is never cancelled to send a heartbeat: cancelling an async generator's
    __anext__ ends the generator, which would silently close the stream after the first quiet
    stretch (a 3B model can think for longer than the heartbeat interval).
    """
    yield "retry: 60000\n\n"
    it = log.follow(after).__aiter__()
    pending: asyncio.Future | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(it.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=heartbeat_s)
            if not done:
                yield ": ping\n\n"
                continue
            try:
                env = pending.result()
            except StopAsyncIteration:
                return
            finally:
                pending = None if pending.done() else pending
            yield sse_frame(env)
    finally:
        if pending is not None and not pending.done():
            pending.cancel()


class Streams:
    """Registry of live and finished logs, falling back to the database after a restart."""

    def __init__(self, store: Store):
        self.store = store
        self._logs: dict[str, EventLog] = {}

    def create(self, stream_id: str, terminal: set[str]) -> EventLog:
        log = EventLog(stream_id, self.store, terminal)
        self._logs[stream_id] = log
        return log

    def get(self, stream_id: str, terminal: set[str], *, resumable: bool = False) -> EventLog | None:
        """resumable: a long-lived log (a chat) that may get new events after a restart."""
        if stream_id in self._logs:
            return self._logs[stream_id]
        events = self.store.events(stream_id)
        if not events:
            return None
        log = EventLog(stream_id, self.store, terminal, existing=events)
        log.done = not resumable  # nothing is producing into a finished run's log loaded from disk
        self._logs[stream_id] = log
        return log
