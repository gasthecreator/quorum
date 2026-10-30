import asyncio

from quorum.store import Store
from quorum.streams import RUN_TERMINAL, EventLog, sse_body


async def collect(log, **kw):
    return [f async for f in sse_body(log, **kw)]


async def test_a_quiet_stretch_longer_than_the_heartbeat_does_not_end_the_stream():
    """Regression: wait_for() on the generator killed it after the first heartbeat."""
    log = EventLog("s", Store(":memory:"), RUN_TERMINAL)
    log.append("run_started", {"x": 1})

    async def later():
        await asyncio.sleep(0.35)  # seven heartbeats of silence at 0.05 s
        log.append("round_started", {"round": 0})
        await asyncio.sleep(0.12)
        log.append("final", {"ok": True})

    task = asyncio.create_task(later())
    frames = await asyncio.wait_for(collect(log, heartbeat_s=0.05), timeout=5)
    await task
    events = [f.split("\n")[1] for f in frames if f.startswith("id:")]
    assert events == ["event: run_started", "event: round_started", "event: final"]
    assert sum(f == ": ping\n\n" for f in frames) >= 3


async def test_finished_logs_replay_and_resume_from_a_sequence_number():
    log = EventLog("s2", Store(":memory:"), RUN_TERMINAL)
    for e in ("run_started", "tally", "final"):
        log.append(e, {})
    assert len([f for f in await collect(log) if f.startswith("id:")]) == 3
    assert [f.split("\n")[0] for f in await collect(log, after=2) if f.startswith("id:")] == ["id: 3"]


async def test_abandoning_the_stream_cancels_the_pending_read():
    log = EventLog("s3", Store(":memory:"), RUN_TERMINAL)
    gen = sse_body(log, heartbeat_s=0.05)
    await gen.__anext__()  # retry line
    await gen.__anext__()  # first heartbeat: a read is now pending
    await gen.aclose()
