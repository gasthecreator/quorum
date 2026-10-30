"""Debate engine (B4) and judge (B6).

Flow: round 0 is blind; rounds 1..N show each model its peers' anonymised answers. A model may only
change its position by stating evidence, otherwise the change is rejected. The run stops on
consensus, stalemate, the round cap or the budget. Findings are then clustered and tallied with
calibrated weights, and a judge rules on each cluster; the judge may override the vote only with a
stated reason, and the override is flagged.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import BaseModel

from . import prompts
from .cluster import build_tally, finding_match
from .config import Settings
from .packs import PackStore
from .registry import Registry
from .runner import CallResult, Runner
from .schemas import (
    ClusterRuling,
    DebaterOutput,
    Dissent,
    Final,
    FinalVerdict,
    Finding,
    FindingDraft,
    JudgeOutput,
    JudgeRuling,
    ModelMessage,
    Pack,
    Position,
    RoundFinished,
    RoundStarted,
    RunRequest,
    RunStarted,
    Stopped,
    Tally,
    Usage,
)
from .store import Store
from .weights import WeightBook

ALIASES = "ABCDEFGH"
Emit = Callable[[str, BaseModel], None]


def noop_emit(event: str, data: BaseModel) -> None:
    return None


@dataclass
class State:
    out: DebaterOutput
    findings: list[Finding]


@dataclass
class Ctx:
    run_id: str
    pack: Pack
    mode: str
    text: str
    nonce: str
    models: list[str]  # in alias order
    aliases: dict[str, str]
    judge_id: str
    weights: WeightBook
    category: str | None
    max_rounds: int
    flags: list[str]
    emit: Emit
    started: float = field(default_factory=time.monotonic)
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def usage(self) -> Usage:
        return Usage(calls=self.calls, prompt_tokens=self.prompt_tokens,
                     completion_tokens=self.completion_tokens,
                     elapsed_ms=int((time.monotonic() - self.started) * 1000))


@dataclass
class DebateResult:
    run_id: str
    mode: str
    aliases: dict[str, str]
    final: Final | None
    tally: Tally | None
    ruling: JudgeRuling | None
    stop: Stopped | None
    rounds: list[dict[str, State]]  # per round, only models that answered that round
    flips: list[dict]
    errors: dict[str, str]


class DebateError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class Debate:
    def __init__(self, registry: Registry, runner: Runner, packs: PackStore, store: Store,
                 settings: Settings):
        self.registry, self.runner, self.packs = registry, runner, packs
        self.store, self.settings = store, settings

    # ------------------------------------------------------------------ setup

    def context(self, req: RunRequest, run_id: str, emit: Emit = noop_emit,
                weights: WeightBook | None = None) -> Ctx:
        pack = self.packs.get(req.pack)
        mode = pack.modes[0]
        self.registry.require(req.model_ids)
        judge = req.judge_model_id or self.registry.default_judge() or req.model_ids[0]
        self.registry.require([judge])
        rng = random.Random(req.seed if req.seed is not None else run_id)
        order = list(dict.fromkeys(req.model_ids))
        rng.shuffle(order)
        return Ctx(
            run_id=run_id, pack=pack, mode=mode, text=req.input, nonce=prompts.new_nonce(),
            models=order, aliases={m: ALIASES[i] for i, m in enumerate(order)}, judge_id=judge,
            weights=weights or WeightBook(self.store.profile_cells(), self.settings.default_weight),
            category=req.category, max_rounds=min(req.max_rounds, 3),
            flags=prompts.scan_for_injection(req.input), emit=emit,
        )

    async def _ask(self, ctx: Ctx, model_id: str, messages: list[dict], cls, schema) -> CallResult:
        res = await self.runner.call(self.registry.provider(model_id), messages, cls, schema)
        ctx.calls += res.attempts
        ctx.prompt_tokens += res.prompt_tokens
        ctx.completion_tokens += res.completion_tokens
        return res

    # ------------------------------------------------------------------ sanitising

    def sanitize(self, mode: str, alias: str, rnd: int, out: DebaterOutput) -> State:
        v = out.position.verdict
        allowed = {"issue", "no_issue", "unsure"} if mode == "review" else {"true", "false", "unsure"}
        findings: list[Finding] = []
        if mode == "review":
            for i, f in enumerate(out.findings, 1):
                if f.claim.strip():
                    findings.append(Finding(**f.model_dump(), id=f"{alias}{rnd}-{i}"))
            if findings and v != "unsure":
                v = "issue"  # a model that lists defects is voting that there is an issue
            elif v == "issue" and not findings:
                v = "unsure"
        if v not in allowed:
            v = "unsure"
        pos = Position(verdict=v, confidence=out.position.confidence)
        drafts = [FindingDraft(**f.model_dump(exclude={"id", "tool_verified"})) for f in findings]
        return State(out.model_copy(update={"position": pos, "findings": drafts}), findings)

    def _differs(self, a: State, b: State) -> bool:
        if a.out.position.verdict != b.out.position.verdict:
            return True
        tol = self.settings.line_tolerance
        return any(not any(finding_match(x, y, tol) for y in b.findings) for x in a.findings) or \
            any(not any(finding_match(y, x, tol) for x in a.findings) for y in b.findings)

    # ------------------------------------------------------------------ agreement

    def agreement(self, ctx: Ctx, states: dict[str, State]) -> float:
        ids = list(states)
        if len(ids) < 2:
            return 1.0
        tol = self.settings.line_tolerance
        scores = []
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = states[ids[i]], states[ids[j]]
                if ctx.mode == "claim" or (not a.findings and not b.findings):
                    scores.append(1.0 if a.out.position.verdict == b.out.position.verdict else 0.0)
                    continue
                inter = sum(1 for x in a.findings if any(finding_match(x, y, tol) for y in b.findings))
                union = len(a.findings) + len(b.findings) - inter
                scores.append(inter / union if union else 1.0)
        return sum(scores) / len(scores)

    def _history_summary(self, ctx: Ctx, rounds: list[dict[str, State]], upto: int) -> str:
        lines = []
        for r in range(upto):
            parts = []
            for m in ctx.models:
                st = rounds[r].get(m) if r < len(rounds) else None
                if not st:
                    continue
                where = ", ".join(f"L{f.line_start} {f.category}" for f in st.findings)
                parts.append(f"{ctx.aliases[m]}={st.out.position.verdict}"
                             f"({st.out.position.confidence:.2f}){' ' + where if where else ''}")
            lines.append(f"Round {r}: " + "; ".join(parts))
        return "\n".join(lines)

    async def blind_one(self, pack: Pack, mode: str, text: str, model_id: str,
                        nonce: str | None = None) -> tuple[State | None, CallResult]:
        """One independent answer from one model (calibration, evaluation baselines)."""
        msgs = prompts.blind_messages(pack, mode, text, nonce or prompts.new_nonce())
        res = await self.runner.call(self.registry.provider(model_id), msgs, DebaterOutput,
                                     prompts.debater_schema())
        state = self.sanitize(mode, "X", 0, res.output) if res.output else None
        return state, res

    # ------------------------------------------------------------------ rounds

    async def _round(self, ctx: Ctx, rnd: int, states: dict[str, State],
                     rounds: list[dict[str, State]], errors: dict[str, str],
                     flips: list[dict], blind: dict[str, CallResult] | None) -> bool:
        """Run one round. Returns True if any model changed its mind."""
        ctx.emit("round_started", RoundStarted(round=rnd))
        schema = prompts.debater_schema()
        answered: dict[str, State] = {}
        any_changed = False

        async def ask(m: str):
            if rnd == 0:
                if blind and m in blind:
                    return m, blind[m]
                msgs = prompts.blind_messages(ctx.pack, ctx.mode, ctx.text, ctx.nonce)
            else:
                peers = [(ctx.aliases[p], states[p].out) for p in ctx.models
                         if p != m and p in states]
                msgs = prompts.debate_messages(
                    ctx.pack, ctx.mode, ctx.text, ctx.nonce, round_no=rnd, own_prev=states[m].out,
                    peers=peers, history_summary=self._history_summary(ctx, rounds, rnd - 1))
            return m, await self._ask(ctx, m, msgs, DebaterOutput, schema)

        targets = [m for m in ctx.models if rnd == 0 or m in states]
        tasks = [asyncio.create_task(ask(m)) for m in targets]
        for fut in asyncio.as_completed(tasks):
            m, res = await fut
            alias = ctx.aliases[m]
            if res.output is None:
                errors[m] = res.error or "no answer"
                prev = states.get(m)
                pos = prev.out.position if prev else Position(verdict="unsure", confidence=0.0)
                ctx.emit("model_message", ModelMessage(
                    round=rnd, model_id=m, alias=alias, position=pos,
                    findings=prev.findings if prev else [], changed=False, changed_reason="",
                    text="", error=errors[m], prompt_tokens=res.prompt_tokens,
                    completion_tokens=res.completion_tokens))
                continue
            errors.pop(m, None)
            new = self.sanitize(ctx.mode, alias, rnd, res.output)
            changed, reason = False, ""
            if rnd > 0:
                prev = states[m]
                if self._differs(prev, new):
                    why = res.output.changed_reason.strip()
                    if len(why) >= self.settings.min_change_reason_chars:
                        changed, reason = True, why
                    else:  # no evidence offered: the change does not count
                        new = State(prev.out.model_copy(update={"text": new.out.text}), prev.findings)
                        reason = "Change rejected: no evidence was given."
                if changed:
                    flips.append({"round": rnd, "model_id": m,
                                  "from": prev.out.position.verdict, "to": new.out.position.verdict})
                    any_changed = True
            states[m] = new
            answered[m] = new
            ctx.emit("model_message", ModelMessage(
                round=rnd, model_id=m, alias=alias, position=new.out.position, findings=new.findings,
                changed=changed, changed_reason=reason, text=new.out.text, error=None,
                prompt_tokens=res.prompt_tokens, completion_tokens=res.completion_tokens))
        rounds.append(answered)
        ctx.emit("round_finished", RoundFinished(round=rnd, agreement=self.agreement(ctx, states)))
        return any_changed

    def _over_budget(self, ctx: Ctx) -> bool:
        spent = ctx.prompt_tokens + ctx.completion_tokens
        return spent >= self.settings.run_token_budget or \
            time.monotonic() - ctx.started >= self.settings.run_time_budget_s

    # ------------------------------------------------------------------ main entry

    async def run(self, req: RunRequest, run_id: str, emit: Emit,
                  blind: dict[str, CallResult] | None = None,
                  weights: WeightBook | None = None) -> DebateResult:
        ctx = self.context(req, run_id, emit, weights)
        emit("run_started", RunStarted(
            run_id=run_id, pack=ctx.pack.id, mode=ctx.mode, model_ids=ctx.models,
            aliases=ctx.aliases, judge_model_id=ctx.judge_id, max_rounds=ctx.max_rounds,
            input_flags=ctx.flags))
        states: dict[str, State] = {}
        rounds: list[dict[str, State]] = []
        errors: dict[str, str] = {}
        flips: list[dict] = []

        await self._round(ctx, 0, states, rounds, errors, flips, blind)
        if not states:
            raise DebateError("all_models_failed",
                              "No model produced a valid answer: " + "; ".join(
                                  f"{ctx.aliases[m]}: {e}" for m, e in errors.items()))
        stop = self._check_stop(ctx, 0, states, changed=True)
        rnd = 0
        while stop is None:
            rnd += 1
            changed = await self._round(ctx, rnd, states, rounds, errors, flips, None)
            stop = self._check_stop(ctx, rnd, states, changed=changed)
        emit("stopped", stop)

        tally, ruling, final = await self.adjudicate(ctx, states)
        emit("tally", tally)
        emit("judge_ruling", ruling)
        emit("final", final)
        return DebateResult(run_id, ctx.mode, ctx.aliases, final, tally, ruling, stop, rounds,
                            flips, errors)

    def _check_stop(self, ctx: Ctx, rnd: int, states: dict[str, State], *,
                    changed: bool) -> Stopped | None:
        if self.agreement(ctx, states) >= 0.999:
            return Stopped(reason="consensus", round=rnd)
        if rnd >= 1 and not changed:
            return Stopped(reason="stalemate", round=rnd)
        if rnd >= ctx.max_rounds:
            return Stopped(reason="max_rounds", round=rnd)
        if self._over_budget(ctx):
            return Stopped(reason="budget", round=rnd)
        return None

    # ------------------------------------------------------------------ tally + judge

    async def adjudicate(self, ctx: Ctx, states: dict[str, State]) -> tuple[Tally, JudgeRuling, Final]:
        tally = build_tally(
            mode=ctx.mode, pack_id=ctx.pack.id, pack_categories=ctx.pack.categories,
            outputs={m: s.out for m, s in states.items()},
            findings={m: s.findings for m, s in states.items()}, aliases=ctx.aliases,
            weights=ctx.weights, category_hint=ctx.category,
            threshold=self.settings.vote_threshold, tol=self.settings.line_tolerance)
        ruling = await self._judge(ctx, tally)
        return tally, ruling, self._finalize(ctx, tally, ruling, states)

    async def _judge(self, ctx: Ctx, tally: Tally) -> JudgeRuling:
        by_id = {c.id: c for c in tally.clusters}
        follow = [ClusterRuling(cluster_id=c.id, accept=c.vote_accept, vote_accept=c.vote_accept,
                                overrides_vote=False) for c in tally.clusters]
        if not tally.clusters:
            return JudgeRuling(judge_model_id=ctx.judge_id, rulings=[], overrides_vote=False,
                               reason="Nothing to rule on.", injection_flags=ctx.flags)
        view = json.dumps({"clusters": [{
            "id": c.id, "kind": c.kind, "verdict": c.verdict, "category": c.category,
            "lines": [c.line_start, c.line_end] if c.kind == "finding" else None,
            "claim": c.claim, "evidence": c.evidence[:300], "vote_accept": c.vote_accept,
            "weighted_score": round(c.weighted_score, 3), "support_share": round(c.support_share, 3),
            "supporters": [{"alias": s.alias, "confidence": s.confidence, "weight": round(s.weight, 3)}
                           for s in c.supporters],
        } for c in tally.clusters], "threshold": tally.threshold}, ensure_ascii=False)
        msgs = prompts.judge_messages(ctx.mode, ctx.text, ctx.nonce, view, ctx.flags)
        res = await self._ask(ctx, ctx.judge_id, msgs, JudgeOutput, prompts.judge_schema())
        if res.output is None:
            return JudgeRuling(judge_model_id=ctx.judge_id, rulings=follow, overrides_vote=False,
                               reason="Judge unavailable; the weighted vote stands.",
                               injection_flags=ctx.flags, error=res.error)
        given = {r.cluster_id: r for r in res.output.rulings if r.cluster_id in by_id}
        rulings: list[ClusterRuling] = []
        for c in tally.clusters:
            r = given.get(c.id)
            accept, reason = (c.vote_accept, "") if r is None else (r.accept, r.reason.strip())
            if accept != c.vote_accept and len(reason) < self.settings.min_change_reason_chars:
                accept, reason = c.vote_accept, "Override ignored: the judge gave no reason."
            rulings.append(ClusterRuling(cluster_id=c.id, accept=accept, vote_accept=c.vote_accept,
                                         overrides_vote=accept != c.vote_accept, reason=reason))
        if ctx.mode == "claim":  # a claim cannot be both true and false
            acc = [r for r in rulings if r.accept]
            if len(acc) > 1:
                keep = max(acc, key=lambda r: by_id[r.cluster_id].weighted_score)
                for r in acc:
                    if r is not keep:
                        r.accept, r.overrides_vote = False, r.vote_accept
                        r.reason = "A claim cannot be both true and false; the higher score stands."
        over = any(r.overrides_vote for r in rulings)
        return JudgeRuling(
            judge_model_id=ctx.judge_id, rulings=rulings, overrides_vote=over,
            reason=res.output.reason if over else "", injection_flags=ctx.flags)

    def _finalize(self, ctx: Ctx, tally: Tally, ruling: JudgeRuling, states: dict[str, State]) -> Final:
        accepted = {r.cluster_id for r in ruling.rulings if r.accept}
        confirmed = [c for c in tally.clusters if c.id in accepted]
        disputed = [c for c in tally.clusters if c.id not in accepted]
        if ctx.mode == "claim":
            overall = confirmed[0].verdict if confirmed and confirmed[0].verdict else "unsure"
        else:
            overall = "issue" if confirmed else "no_issue"
        backers = {s.model_id for c in confirmed for s in c.supporters}
        dissent: list[Dissent] = []
        for m in ctx.models:
            st = states.get(m)
            if st is None:
                continue
            v = st.out.position.verdict
            if ctx.mode == "claim":
                dissents = v in ("true", "false") and v != overall
            else:
                dissents = v != overall or (overall == "issue" and m not in backers)
            if dissents:
                dissent.append(Dissent(
                    model_id=m, alias=ctx.aliases[m], verdict=v,
                    confidence=st.out.position.confidence,
                    finding_ids=[f.id for f in st.findings], reason=st.out.text[:400]))
        n_over = sum(r.overrides_vote for r in ruling.rulings)
        summary = (f"{len(confirmed)} confirmed, {len(disputed)} disputed, "
                   f"{len(dissent)} dissenting; the judge "
                   + (f"overrode the weighted vote on {n_over} cluster(s)." if n_over
                      else "agreed with the weighted vote."))
        return Final(run_id=ctx.run_id, verdict=FinalVerdict(overall=overall, summary=summary),
                     confirmed_findings=confirmed, disputed_findings=disputed, dissent=dissent,
                     usage=ctx.usage())
