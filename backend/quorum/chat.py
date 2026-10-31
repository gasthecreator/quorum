"""Group chat (contract v1.4): one human message starts a turn in which every panelist replies in the
thread, may react to the others, and a judge posts a short summary. Follow-ups continue the thread.

Panelists see each other as aliases (A, B, C), like the debate engine; the UI shows real model names.
Chat does not use calibrated vote weights: nothing is being scored, the judge just summarises.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from dataclasses import dataclass, field

from . import prompts
from .calibrate import now
from .config import Settings
from .engine import ALIASES
from .registry import Registry
from .runner import Runner
from .schemas import (
    ChatCreate,
    ChatReply,
    ChatStarted,
    ErrorEvent,
    JudgeMessage,
    ModelTyping,
    TurnFinished,
    Usage,
    UserMessage,
)
from .store import Store
from .streams import CHAT_TERMINAL, EventLog, Streams


class ChatError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


@dataclass
class ChatState:
    chat_id: str
    title: str
    model_ids: list[str]
    aliases: dict[str, str]
    judge_model_id: str
    rounds: int
    created_at: str
    busy: bool = False
    turns: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ChatManager:
    def __init__(self, registry: Registry, runner: Runner, store: Store, streams: Streams,
                 settings: Settings):
        self.registry, self.runner, self.store = registry, runner, store
        self.streams, self.settings = streams, settings
        self._chats: dict[str, ChatState] = {}

    # ------------------------------------------------------------------ lifecycle

    def create(self, req: ChatCreate) -> ChatState:
        self.registry.require(req.model_ids + ([req.judge_model_id] if req.judge_model_id else []))
        order = list(dict.fromkeys(req.model_ids))
        random.Random(uuid.uuid4().int).shuffle(order)
        judge = req.judge_model_id or order[0]
        for m in order:
            rec = self.store.get_model(m)
            if rec and rec["status"] in ("unavailable", "error"):
                raise ChatError("model_unavailable", f"{m} is {rec['status']}: {rec['status_detail']}", 409)
        chat_id = uuid.uuid4().hex[:10]
        title = (req.title or (req.message or "New chat").strip().split("\n")[0])[:80]
        created = now()
        st = ChatState(chat_id, title, order, {m: ALIASES[i] for i, m in enumerate(order)}, judge,
                       req.rounds, created)
        self.store.create_chat(chat_id, {"title": title, "model_ids": order, "aliases": st.aliases,
                                         "judge_model_id": judge, "rounds": req.rounds}, created)
        self._chats[chat_id] = st
        log = self.streams.create(chat_id, CHAT_TERMINAL)
        log.append("chat_started", ChatStarted(chat_id=chat_id, title=title, model_ids=order,
                                               aliases=st.aliases, judge_model_id=judge, rounds=req.rounds))
        return st

    def get(self, chat_id: str) -> ChatState:
        if chat_id in self._chats:
            return self._chats[chat_id]
        rec = self.store.get_chat(chat_id)
        if rec is None:
            raise ChatError("unknown_chat", f"No chat '{chat_id}'.", 404)
        log = self.log(chat_id)
        turns = sum(1 for e in log.events if e["event"] == "user_message")
        st = ChatState(chat_id, rec["title"], rec["model_ids"], rec["aliases"], rec["judge_model_id"],
                       rec["rounds"], rec["created_at"], turns=turns)
        self._chats[chat_id] = st
        return st

    def log(self, chat_id: str) -> EventLog:
        log = self.streams.get(chat_id, CHAT_TERMINAL, resumable=True)
        if log is None:  # a chat whose events were never written (should not happen)
            log = self.streams.create(chat_id, CHAT_TERMINAL)
        return log

    # ------------------------------------------------------------------ turns

    def begin_turn(self, chat_id: str, text: str):
        """Validate, record the human's message, and return (turn, message_id, coroutine to run)."""
        st = self.get(chat_id)
        if st.busy:
            raise ChatError("turn_in_progress", "The panel is still replying; wait for turn_finished.", 409)
        st.busy = True
        st.turns += 1
        turn, message_id = st.turns, uuid.uuid4().hex[:8]
        log = self.log(chat_id)
        history = self._history(log)
        if st.title == "New chat":  # an untitled chat is named after its first message
            st.title = text.strip().split("\n")[0][:80]
            self.store.set_chat_title(chat_id, st.title)
        log.append("user_message", UserMessage(turn=turn, message_id=message_id, text=text))
        return turn, message_id, self._run_turn(st, log, turn, text, history)

    @staticmethod
    def _history(log: EventLog) -> list[tuple[str, str]]:
        """(human text, judge answer) for finished turns; falls back to the panelists' replies."""
        humans: dict[int, str] = {}
        answers: dict[int, str] = {}
        replies: dict[int, list[str]] = {}
        finished: set[int] = set()
        for e in log.events:
            d = e["data"]
            if e["event"] == "user_message":
                humans[d["turn"]] = d["text"]
            elif e["event"] == "judge_message" and not d.get("error"):
                answers[d["turn"]] = d.get("answer") or d["text"]
            elif e["event"] == "chat_message" and d["text"] and not d["passed"]:
                replies.setdefault(d["turn"], []).append(f"{d['alias']}: {d['text']}")
            elif e["event"] == "turn_finished":
                finished.add(d["turn"])
        return [(humans[t], answers.get(t) or " | ".join(replies.get(t, []))[:600])
                for t in sorted(finished) if t in humans]

    async def _run_turn(self, st: ChatState, log: EventLog, turn: int, text: str,
                        history: list[tuple[str, str]]) -> None:
        started = asyncio.get_event_loop().time()
        usage = Usage()
        nonce = prompts.new_nonce()
        thread: list[tuple[str, str]] = []
        try:
            for rnd in range(st.rounds + 1):
                if rnd > 0 and not thread:
                    break  # nobody could reply, so there is nothing to react to
                snapshot = list(thread)
                results = await self._round(st, log, turn, rnd, text, history, snapshot, nonce, usage)
                for alias, reply in results:
                    if reply:
                        thread.append((alias, reply))
            await self._judge(st, log, turn, text, history, thread, nonce, usage)
        except Exception as e:  # noqa: BLE001 - the turn must always finish so the chat is not stuck
            log.append("error", ErrorEvent(code="internal_error", message=str(e)[:300]))
        finally:
            usage.elapsed_ms = int((asyncio.get_event_loop().time() - started) * 1000)
            log.append("turn_finished", TurnFinished(turn=turn, usage=usage))
            st.busy = False

    async def _round(self, st, log, turn, rnd, text, history, thread, nonce, usage) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []

        async def one(model_id: str):
            alias = st.aliases[model_id]
            log.append("model_typing", ModelTyping(turn=turn, round=rnd, model_id=model_id, alias=alias,
                                                   role="panelist"))
            others = [a for m, a in st.aliases.items() if m != model_id]
            msgs = prompts.chat_panelist_messages(alias, others, text, history, thread, nonce)
            res = await self.runner.text(self.registry.provider(model_id), msgs)
            usage.calls += res.attempts
            usage.prompt_tokens += res.prompt_tokens
            usage.completion_tokens += res.completion_tokens
            reply = prompts.clean_chat_text(res.text, alias) if not res.error else ""
            passed = bool(reply) and rnd > 0 and prompts.is_pass(reply)
            if passed:
                reply = ""
            log.append("chat_message", ChatReply(
                turn=turn, round=rnd, message_id=uuid.uuid4().hex[:8], model_id=model_id, alias=alias,
                text=reply, passed=passed, error=res.error, prompt_tokens=res.prompt_tokens,
                completion_tokens=res.completion_tokens))
            return alias, reply

        for fut in asyncio.as_completed([asyncio.create_task(one(m)) for m in st.model_ids]):
            out.append(await fut)
        return sorted(out, key=lambda x: x[0])

    async def _judge(self, st, log, turn, text, history, thread, nonce, usage) -> None:
        jid = st.judge_model_id
        if not thread:
            log.append("error", ErrorEvent(code="all_models_failed",
                                           message="No participant produced a reply this turn."))
            return
        log.append("model_typing", ModelTyping(turn=turn, round=0, model_id=jid,
                                               alias=st.aliases.get(jid, "judge"), role="judge"))
        msgs = prompts.chat_judge_messages(text, history, thread, nonce)
        res = await self.runner.text(self.registry.provider(jid), msgs)
        usage.calls += res.attempts
        usage.prompt_tokens += res.prompt_tokens
        usage.completion_tokens += res.completion_tokens
        if res.error:
            log.append("judge_message", JudgeMessage(turn=turn, message_id=uuid.uuid4().hex[:8], model_id=jid,
                                                     text="", error=res.error))
            return
        parts = prompts.parse_judge_lines(res.text)
        log.append("judge_message", JudgeMessage(
            turn=turn, message_id=uuid.uuid4().hex[:8], model_id=jid, text=prompts.clean_chat_text(res.text),
            **parts))
