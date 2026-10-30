"""HTTP API (B9). Every route and payload here is part of the frozen contract."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import __version__
from .calibrate import calibrate_many, now
from .chat import ChatError, ChatManager
from .config import Settings
from .engine import Debate, DebateError
from .evaluate import run_eval
from .export import to_html, to_markdown
from .packs import PackError, PackStore
from .providers.ollama import ollama_version
from .registry import Registry, RegistryError
from .runner import Runner
from .schemas import (
    CONTRACT_VERSION,
    ApiError,
    CalibrateRequest,
    ChatCreate,
    ChatCreated,
    ChatListItem,
    ChatMessageAccepted,
    ChatMessageRequest,
    ChatSummary,
    ErrorEvent,
    EvalRequest,
    EvalResult,
    EventEnvelope,
    Final,
    Health,
    Job,
    JobCreated,
    JobDone,
    JobError,
    JobProgress,
    ModelCreate,
    ModelCreated,
    ModelInfo,
    OllamaHealth,
    Pack,
    ProfilesResponse,
    RunCreated,
    RunRequest,
    RunSummary,
)
from .store import Store
from .streams import JOB_TERMINAL, RUN_TERMINAL, EventLog, Streams, sse_body
from .weights import WeightBook


class ApiException(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


class AppState:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = Store(settings.db_path)
        self.store.fail_orphaned_runs()
        self.registry = Registry(settings, self.store)
        self.packs = PackStore(settings.pack_dirs)
        self.runner = Runner(settings)
        self.debate = Debate(self.registry, self.runner, self.packs, self.store, settings)
        self.streams = Streams(self.store)
        self.chats = ChatManager(self.registry, self.runner, self.store, self.streams, settings)
        self.tasks: set[asyncio.Task] = set()

    def spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    state = AppState(settings)
    state.registry.load_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        for t in list(state.tasks):
            t.cancel()
        await state.registry.aclose()

    app = FastAPI(title="Quorum", version=CONTRACT_VERSION, lifespan=lifespan)
    app.state.q = state
    app.add_middleware(
        CORSMiddleware, allow_origins=list(settings.cors_origins), allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Last-Event-ID"])

    @app.exception_handler(ApiException)
    async def _api_exc(_: Request, e: ApiException):
        return JSONResponse(ApiError(code=e.code, message=e.message).model_dump(), e.status)

    @app.exception_handler(RegistryError)
    async def _reg_exc(_: Request, e: RegistryError):
        return JSONResponse(ApiError(code=e.code, message=e.message).model_dump(), e.status)

    @app.exception_handler(ChatError)
    async def _chat_exc(_: Request, e: ChatError):
        return JSONResponse(ApiError(code=e.code, message=e.message).model_dump(), e.status)

    @app.exception_handler(PackError)
    async def _pack_exc(_: Request, e: PackError):
        return JSONResponse(ApiError(code="pack_error", message=str(e)).model_dump(), 404)

    @app.exception_handler(RequestValidationError)
    async def _val_exc(_: Request, e: RequestValidationError):
        first = e.errors()[0]
        where = ".".join(str(x) for x in first["loc"] if x != "body")
        return JSONResponse(ApiError(code="validation_error",
                                     message=f"{where}: {first['msg']}").model_dump(), 422)

    # ------------------------------------------------------------------ meta

    @app.get("/health", response_model=Health)
    async def health():
        version = await ollama_version(settings.ollama_url)
        return Health(ok=True, version=__version__, contract_version=CONTRACT_VERSION,
                      ollama=OllamaHealth(reachable=version is not None, base_url=settings.ollama_url,
                                          version=version),
                      models=len(state.store.list_models()),
                      fake_models_enabled=settings.enable_fake,
                      grounding={"semgrep": state.debate.grounder is not None})

    @app.get("/packs", response_model=list[Pack])
    async def packs():
        return state.packs.list()

    # ------------------------------------------------------------------ models

    @app.get("/models", response_model=list[ModelInfo])
    async def models():
        return await state.registry.list()

    @app.post("/models", response_model=ModelCreated, status_code=201)
    async def add_model(body: ModelCreate):
        info = await state.registry.add(body)
        job_id = None
        if body.calibrate:
            pack_ids = body.packs or [p.id for p in state.packs.list()]
            for p in pack_ids:
                state.packs.get(p)  # 404 early on a bad pack id
            if pack_ids:
                job_id = _start_calibration(info.id, [info.id], pack_ids, None)
                info = state.registry.info(info.id)
        return ModelCreated(model=info, job_id=job_id)

    @app.get("/profiles", response_model=ProfilesResponse)
    async def profiles(pack: str | None = None):
        book = WeightBook(state.store.profile_cells(), settings.default_weight)
        data = book.profiles()
        if pack:
            data = {m: {pack: pk[pack]} for m, pk in data.items() if pack in pk}
        return ProfilesResponse(profiles=data, default_weight=settings.default_weight)

    # ------------------------------------------------------------------ jobs

    def _start_job(kind: str, work, total: int = 0) -> str:
        job_id = uuid.uuid4().hex[:10]
        state.store.create_job(job_id, kind, now(), total)
        log = state.streams.create(job_id, JOB_TERMINAL)

        def progress(done: int, total_: int, message: str = "") -> None:
            state.store.update_job(job_id, status="running", done=done, total=total_, message=message)
            log.append("job_progress", JobProgress(job_id=job_id, done=done, total=total_,
                                                   message=message))

        async def go():
            state.store.update_job(job_id, status="running")
            try:
                result = await work(progress)
            except Exception as e:  # noqa: BLE001 - surfaced to the client as a job error
                state.store.update_job(job_id, status="error", error=str(e)[:500])
                log.append("job_error", JobError(job_id=job_id, code=type(e).__name__, message=str(e)))
                return
            state.store.update_job(job_id, status="done", result=result)
            log.append("job_done", JobDone(job_id=job_id, result=result))

        state.spawn(go())
        return job_id

    def _start_calibration(_label: str, model_ids: list[str], pack_ids: list[str],
                           limit: int | None) -> str:
        for m in model_ids:
            state.store.set_model_status(m, "calibrating", "")

        async def work(progress):
            return await calibrate_many(state.debate, model_ids, pack_ids, limit=limit,
                                        progress=progress)

        return _start_job("calibrate", work)

    @app.post("/calibrate", response_model=JobCreated, status_code=202)
    async def calibrate(body: CalibrateRequest):
        state.registry.require(body.model_ids)
        state.packs.get(body.pack)
        return JobCreated(job_id=_start_calibration("calibrate", body.model_ids, [body.pack],
                                                    body.limit))

    @app.post("/eval", response_model=JobCreated, status_code=202)
    async def start_eval(body: EvalRequest):
        state.registry.require(body.model_ids)
        state.packs.get(body.pack)

        async def work(progress):
            res = await run_eval(state.debate, body, progress)
            return res.model_dump(mode="json")

        return JobCreated(job_id=_start_job("eval", work))

    @app.get("/eval/latest", response_model=EvalResult)
    async def eval_latest(pack: str | None = None):
        latest = state.store.latest_eval(pack)
        if latest is None:
            raise ApiException(404, "no_eval", "No evaluation has been run yet.")
        return latest

    @app.get("/jobs/{job_id}", response_model=Job)
    async def job(job_id: str):
        j = state.store.get_job(job_id)
        if j is None:
            raise ApiException(404, "unknown_job", f"No job '{job_id}'.")
        return Job(**{k: j[k] for k in ("id", "kind", "status", "done", "total", "message",
                                         "result", "error")})

    @app.get("/jobs/{job_id}/events")
    async def job_events(job_id: str, last_event_id: str | None = Header(default=None),
                         after: int = Query(default=0, ge=0)):
        log = state.streams.get(job_id, JOB_TERMINAL)
        if log is None:
            if state.store.get_job(job_id) is None:
                raise ApiException(404, "unknown_job", f"No job '{job_id}'.")
            log = state.streams.create(job_id, JOB_TERMINAL)
        return _sse(log, last_event_id, after)

    # ------------------------------------------------------------------ runs

    def _sse(log: EventLog, last_event_id: str | None, after: int) -> StreamingResponse:
        start = int(last_event_id) if last_event_id and last_event_id.isdigit() else after
        return StreamingResponse(
            sse_body(log, start), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/runs", response_model=RunCreated, status_code=202)
    async def start_run(body: RunRequest):
        state.packs.get(body.pack)
        state.registry.require(body.model_ids + ([body.judge_model_id] if body.judge_model_id else []))
        for m in body.model_ids:
            rec = state.store.get_model(m)
            if rec["status"] in ("unavailable", "error"):
                raise ApiException(409, "model_unavailable",
                                   f"{m} is {rec['status']}: {rec['status_detail']}")
        run_id = uuid.uuid4().hex[:10]
        state.store.create_run(run_id, body.model_dump(), now())
        log = state.streams.create(run_id, RUN_TERMINAL)

        async def go():
            try:
                result = await state.debate.run(body, run_id, log.append)
                state.store.finish_run(run_id, "done", result.final.model_dump(mode="json"))
            except DebateError as e:
                log.append("error", ErrorEvent(code=e.code, message=e.message))
                state.store.finish_run(run_id, "error", None)
            except Exception as e:  # noqa: BLE001 - the stream must always terminate
                log.append("error", ErrorEvent(code="internal_error", message=str(e)[:300]))
                state.store.finish_run(run_id, "error", None)

        state.spawn(go())
        return RunCreated(run_id=run_id)

    @app.get("/runs/{run_id}/events")
    async def run_events(run_id: str, last_event_id: str | None = Header(default=None),
                         after: int = Query(default=0, ge=0)):
        log = state.streams.get(run_id, RUN_TERMINAL)
        if log is None:
            raise ApiException(404, "unknown_run", f"No run '{run_id}'.")
        return _sse(log, last_event_id, after)

    @app.get("/runs/{run_id}/export")
    async def export_run(run_id: str, format: Literal["md", "html"] = "md"):
        rec = state.store.get_run(run_id)
        if rec is None:
            raise ApiException(404, "unknown_run", f"No run '{run_id}'.")
        if rec["status"] == "running":
            raise ApiException(409, "run_not_finished", "The run is still going; export it when it ends.")
        run = {"run_id": run_id, "request": rec["request"], "status": rec["status"],
               "events": state.store.events(run_id)}
        body, media = ((to_markdown(run), "text/markdown") if format == "md"
                       else (to_html(run), "text/html"))
        return Response(body, media_type=f"{media}; charset=utf-8", headers={
            "Content-Disposition": f'attachment; filename="quorum-{run_id}.{format}"'})

    @app.get("/runs/{run_id}", response_model=RunSummary)
    async def get_run(run_id: str):
        rec = state.store.get_run(run_id)
        if rec is None:
            raise ApiException(404, "unknown_run", f"No run '{run_id}'.")
        return RunSummary(
            run_id=run_id, status=rec["status"], request=RunRequest(**rec["request"]),
            created_at=rec["created_at"],
            events=[EventEnvelope(**e) for e in state.store.events(run_id)],
            final=Final(**rec["final"]) if rec["final"] else None)

    # ------------------------------------------------------------------ group chat (v1.4)

    @app.post("/chats", response_model=ChatCreated, status_code=201)
    async def create_chat(body: ChatCreate):
        st = state.chats.create(body)
        if body.message:
            turn, message_id, coro = state.chats.begin_turn(st.chat_id, body.message)
            state.spawn(coro)
            return ChatCreated(chat_id=st.chat_id, turn=turn, message_id=message_id)
        return ChatCreated(chat_id=st.chat_id)

    @app.get("/chats", response_model=list[ChatListItem])
    async def list_chats():
        out = []
        for rec in state.store.list_chats():
            events = state.chats.log(rec["id"]).events
            last = next((e["data"]["text"] for e in reversed(events)
                         if e["event"] in ("judge_message", "chat_message", "user_message")
                         and e["data"].get("text")), "")
            out.append(ChatListItem(
                chat_id=rec["id"], title=rec["title"], model_ids=rec["model_ids"],
                created_at=rec["created_at"], last_message=last[:200],
                turns=sum(1 for e in events if e["event"] == "user_message")))
        return out

    @app.get("/chats/{chat_id}", response_model=ChatSummary)
    async def get_chat(chat_id: str):
        st = state.chats.get(chat_id)
        return ChatSummary(
            chat_id=chat_id, title=st.title, model_ids=st.model_ids, aliases=st.aliases,
            judge_model_id=st.judge_model_id, rounds=st.rounds, created_at=st.created_at,
            status="replying" if st.busy else "idle",
            events=[EventEnvelope(**e) for e in state.chats.log(chat_id).events])

    @app.post("/chats/{chat_id}/messages", response_model=ChatMessageAccepted, status_code=202)
    async def post_chat_message(chat_id: str, body: ChatMessageRequest):
        turn, message_id, coro = state.chats.begin_turn(chat_id, body.text)
        state.spawn(coro)
        return ChatMessageAccepted(chat_id=chat_id, turn=turn, message_id=message_id)

    @app.get("/chats/{chat_id}/events")
    async def chat_events(chat_id: str, last_event_id: str | None = Header(default=None),
                          after: int = Query(default=0, ge=0)):
        state.chats.get(chat_id)  # 404 for an unknown chat
        return _sse(state.chats.log(chat_id), last_event_id, after)

    return app
