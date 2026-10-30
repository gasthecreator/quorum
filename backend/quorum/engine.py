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
from .cluster import build_tally, finding_match, group_findings, spans_overlap
from .config import Settings
from .grounding import Grounder, default_grounder, timed_check
from .packs import PackStore
from .registry import Registry
from .roles import assign_roles
from .runner import CallResult, Runner
from .schemas import (
    Agreement,
    BlindSpot,
    Clash,
    ClashSide,
    ClusterRuling,
    DebaterOutput,
    Dissent,
    Escalated,
    Final,
    FinalVerdict,
    Finding,
    FindingDraft,
    FinishedEarly,
    Framed,
    FramerOutput,
    Grounded,
    JudgeOutput,
    JudgeRuling,
    ModelMessage,
    NextStep,
    Pack,
    Position,
    Recommendation,
    RoleAssignment,
    RoundFinished,
    RoundStarted,
    RunRequest,
    RunStarted,
    Stopped,
    Tally,
    ToolFinding,
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
    escalate: str = "always"
    frame: bool = True
    ground: bool = False
    roles: dict[str, RoleAssignment] = field(default_factory=dict)
    framing: str | None = None
    rng: random.Random = field(default_factory=random.Random)
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
    states: dict[str, State] = field(default_factory=dict)
    ctx: Ctx | None = None


_COMMENT = ("#", "//", "--", "/*", "*")


def _only_comments(source: str, start: int, end: int) -> bool:
    """True if every line in [start, end] of the pasted text is blank or a pure comment.

    A defect has to be in code. Text in a comment (including a note aimed at the reviewers) is not one.
    A diff line carries a leading +, - or space marker, which is ignored.
    """
    lines = source.split("\n")
    if start < 1 or end < start or start > len(lines):
        return False  # unlocatable findings are judged elsewhere, not silently dropped
    for raw in lines[start - 1:end]:
        t = raw.lstrip()
        if t[:1] in "+-" and not t.startswith(("--", "++")):
            t = t[1:].lstrip()
        if t and not t.startswith(_COMMENT):
            return False
    return True


class DebateError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class Debate:
    def __init__(self, registry: Registry, runner: Runner, packs: PackStore, store: Store,
                 settings: Settings, grounder: Grounder | None = None):
        self.registry, self.runner, self.packs = registry, runner, packs
        self.store, self.settings = store, settings
        self.grounder = grounder if grounder is not None else default_grounder()

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
        weights = weights or WeightBook(self.store.profile_cells(), self.settings.default_weight)
        roles: dict[str, RoleAssignment] = {}
        if mode == "review" and (req.roles == "profile" or req.outsider):
            roles = assign_roles(order, pack, weights, outsider=req.outsider)
            if req.roles != "profile":  # outsider only
                roles = {m: r for m, r in roles.items() if r.role == "outsider"}
        escalate = req.escalate if len(order) >= 3 else "always"  # nothing to escalate to
        frame = req.frame if req.frame is not None else escalate == "always"
        return Ctx(
            escalate=escalate, frame=frame, ground=req.ground and mode == "review", roles=roles,
            rng=random.Random(f"{req.seed if req.seed is not None else run_id}-peers"),
            run_id=run_id, pack=pack, mode=mode, text=req.input, nonce=prompts.new_nonce(),
            models=order, aliases={m: ALIASES[i] for i, m in enumerate(order)}, judge_id=judge,
            weights=weights,
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

    def sanitize(self, mode: str, alias: str, rnd: int, out: DebaterOutput,
                 source: str | None = None) -> State:
        v = out.position.verdict
        allowed = {"issue", "no_issue", "unsure"} if mode == "review" else {"true", "false", "unsure"}
        findings: list[Finding] = []
        if mode == "review":
            n_lines = len(source.rstrip("\n").split("\n")) if source else None
            for f in out.findings:
                if not f.claim.strip() or (source and _only_comments(source, f.line_start, f.line_end)):
                    continue  # no claim, or it points at a comment rather than code
                if n_lines is not None:
                    if f.line_start > n_lines:
                        continue  # points past the end of the input: nothing there to be wrong
                    if f.line_end > n_lines:  # seen with gemma3:4b: lines 7-17 in a 10-line diff
                        f = f.model_copy(update={"line_end": n_lines})
                # small models repeat themselves: one defect, one finding
                if any(g.category == f.category and spans_overlap(
                        (g.line_start, g.line_end), (f.line_start, f.line_end),
                        self.settings.line_tolerance) for g in findings):
                    continue
                findings.append(Finding(**f.model_dump(), id=f"{alias}{rnd}-{len(findings) + 1}"))
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
                # cluster the two answers together: agreement is the share of clusters both raised
                groups = group_findings({"a": a.findings, "b": b.findings}, tol,
                                        self.settings.cluster_by_category)
                scores.append(sum(len(g.members) == 2 for g in groups) / len(groups) if groups else 1.0)
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
        state = self.sanitize(mode, "X", 0, res.output, text) if res.output else None
        return state, res

    # ------------------------------------------------------------------ rounds

    async def _round(self, ctx: Ctx, rnd: int, states: dict[str, State],
                     rounds: list[dict[str, State]], errors: dict[str, str],
                     flips: list[dict], blind: dict[str, CallResult] | None, *,
                     targets: list[str] | None = None, begin: bool = True, end: bool = True,
                     answered: dict[str, State] | None = None) -> bool:
        """Run one round (or part of round 0, for escalation). True if any model changed its mind."""
        if begin:
            ctx.emit("round_started", RoundStarted(round=rnd))
        schema = prompts.debater_schema()
        answered = answered if answered is not None else {}
        any_changed = False

        async def ask(m: str):
            if rnd == 0:
                if blind and m in blind:
                    return m, blind[m]
                msgs = prompts.blind_messages(
                    ctx.pack, ctx.mode, ctx.text, ctx.nonce,
                    None if self._role(ctx, m) == "outsider" else ctx.framing, self._role(ctx, m))
            else:
                peers = [(ctx.aliases[p], states[p].out) for p in ctx.models
                         if p != m and p in states]
                ctx.rng.shuffle(peers)  # no model sees the peers in a fixed order
                msgs = prompts.debate_messages(
                    ctx.pack, ctx.mode, ctx.text, ctx.nonce, round_no=rnd, own_prev=states[m].out,
                    peers=peers, history_summary=self._history_summary(ctx, rounds, rnd - 1),
                    framing=ctx.framing, role=self._role(ctx, m))
            return m, await self._ask(ctx, m, msgs, DebaterOutput, schema)

        # the outsider reviews cold in round 0 and never sees peers, so it sits out visible rounds
        targets = [m for m in (targets or ctx.models)
                   if (rnd == 0 or m in states) and not (rnd > 0 and self._role(ctx, m) == "outsider")]
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
                    text="", role=self._role(ctx, m), error=errors[m], prompt_tokens=res.prompt_tokens,
                    completion_tokens=res.completion_tokens))
                continue
            errors.pop(m, None)
            new = self.sanitize(ctx.mode, alias, rnd, res.output, ctx.text)
            changed, reason = False, ""
            if rnd > 0:
                prev = states[m]
                if self._differs(prev, new):
                    why = res.output.changed_reason.strip()
                    if len(why) >= self.settings.min_change_reason_chars:
                        changed, reason = True, why
                    else:  # no evidence offered: the change does not count
                        new = State(prev.out.model_copy(update={
                            "text": new.out.text, "critique": new.out.critique}), prev.findings)
                        reason = "Change rejected: no evidence was given."
                if changed:
                    flips.append({"round": rnd, "model_id": m,
                                  "from": prev.out.position.verdict, "to": new.out.position.verdict})
                    any_changed = True
            states[m] = new
            answered[m] = new
            ctx.emit("model_message", ModelMessage(
                round=rnd, model_id=m, alias=alias, position=new.out.position, findings=new.findings,
                changed=changed, changed_reason=reason, text=new.out.text,
                critique=new.out.critique if rnd > 0 else None, role=self._role(ctx, m), error=None,
                prompt_tokens=res.prompt_tokens, completion_tokens=res.completion_tokens))
        if end:
            rounds.append(answered)
            ctx.emit("round_finished", RoundFinished(round=rnd, agreement=self.agreement(ctx, states)))
        return any_changed

    @staticmethod
    def _role(ctx: Ctx, model_id: str) -> str | None:
        a = ctx.roles.get(model_id)
        return a.role if a else None

    def _over_budget(self, ctx: Ctx) -> bool:
        spent = ctx.prompt_tokens + ctx.completion_tokens
        return spent >= self.settings.run_token_budget or \
            time.monotonic() - ctx.started >= self.settings.run_time_budget_s

    # ------------------------------------------------------------------ main entry

    async def _frame(self, ctx: Ctx) -> None:
        """Framer call (B11): a neutral restatement the debaters see next to the original input."""
        msgs = prompts.framer_messages(ctx.mode, ctx.text, ctx.nonce)
        res = await self._ask(ctx, ctx.judge_id, msgs, FramerOutput, prompts.framer_schema())
        text = res.output.restatement if res.output else ""
        error = res.error if res.output is None else (None if text else "framer returned nothing")
        new_flags: list[str] = []
        if text:
            new_flags = [f for f in prompts.scan_for_injection(text) if f not in ctx.flags]
            ctx.flags.extend(new_flags)
            ctx.framing = text
        ctx.emit("framed", Framed(model_id=ctx.judge_id, restatement=text, error=error,
                                  input_flags=new_flags))

    async def _ground(self, ctx: Ctx) -> list[ToolFinding]:
        """Static analysis over the input (B10). A missing or failing tool never stops the run."""
        g = self.grounder
        if g is None:
            ctx.emit("grounded", Grounded(tool="semgrep", available=False, findings=[],
                                          error="semgrep is not installed (pip install semgrep)"))
            return []
        findings, err, ms = await timed_check(g, ctx.text)
        ctx.emit("grounded", Grounded(tool=g.name, available=True, findings=findings, error=err,
                                      duration_ms=ms))
        return findings

    def _first_pass(self, ctx: Ctx) -> tuple[list[str], list[str]]:
        """Best calibrated model plus the best model from a different family."""
        def pooled(m: str) -> float:
            return ctx.weights.weight(m, ctx.pack.id, None)[0]

        def family(m: str) -> str:
            return (self.store.get_model(m) or {}).get("family", m)

        ranked = sorted(ctx.models, key=lambda m: -pooled(m))  # stable: ties keep alias order
        best = ranked[0]
        others = ranked[1:]
        second = next((m for m in others if family(m) != family(best)), others[0])
        first = [best, second]
        return first, [m for m in ctx.models if m not in first]

    def _early_decision(self, ctx: Ctx, states: dict[str, State], first: list[str]) -> dict:
        got = {m: states[m] for m in first if m in states}
        ag = self.agreement(ctx, got) if len(got) == 2 else 0.0
        confs = [s.out.position.confidence for s in got.values()]
        low = len(got) < 2 or any(
            s.out.position.verdict == "unsure" or s.out.position.confidence
            < self.settings.escalation_min_confidence for s in got.values())
        if len(got) < 2:
            return {"finish": False, "reason": "low_confidence", "agreement": ag,
                    "detail": "Only one of the two first-pass models gave a usable answer."}
        if ag < 0.999:
            return {"finish": False, "reason": "disagreement", "agreement": ag,
                    "detail": "The two first-pass models disagree."}
        if low:
            return {"finish": False, "reason": "low_confidence", "agreement": ag,
                    "detail": "The first-pass models agree but one is unsure or below "
                              f"{self.settings.escalation_min_confidence:.2f} confidence."}
        return {"finish": True, "reason": "The two first-pass models agree with high confidence.",
                "agreement": ag, "min_confidence": min(confs)}

    async def run(self, req: RunRequest, run_id: str, emit: Emit,
                  blind: dict[str, CallResult] | None = None,
                  weights: WeightBook | None = None) -> DebateResult:
        ctx = self.context(req, run_id, emit, weights)
        emit("run_started", RunStarted(
            run_id=run_id, pack=ctx.pack.id, mode=ctx.mode, model_ids=ctx.models,
            aliases=ctx.aliases, judge_model_id=ctx.judge_id, max_rounds=ctx.max_rounds,
            escalate=ctx.escalate, frame=ctx.frame, ground=ctx.ground, roles=ctx.roles,
            input_flags=list(ctx.flags)))
        if ctx.frame:
            await self._frame(ctx)
        states: dict[str, State] = {}
        rounds: list[dict[str, State]] = []
        errors: dict[str, str] = {}
        flips: list[dict] = []
        stop: Stopped | None = None
        early = False

        if ctx.escalate == "auto":
            first, rest = self._first_pass(ctx)
            answered: dict[str, State] = {}
            await self._round(ctx, 0, states, rounds, errors, flips, blind, targets=first,
                              end=False, answered=answered)
            d = self._early_decision(ctx, states, first)
            if d["finish"]:
                rounds.append(answered)
                emit("round_finished", RoundFinished(round=0, agreement=d["agreement"]))
                emit("finished_early", FinishedEarly(
                    reason=d["reason"], model_ids=first, agreement=d["agreement"],
                    min_confidence=d["min_confidence"], calls_saved=len(rest) + 1))
                early = True
            else:
                emit("escalated", Escalated(
                    reason=d["reason"], detail=d["detail"], first_pass_model_ids=first,
                    added_model_ids=rest, agreement=d["agreement"]))
                await self._round(ctx, 0, states, rounds, errors, flips, blind, targets=rest,
                                  begin=False, answered=answered)
        else:
            await self._round(ctx, 0, states, rounds, errors, flips, blind)
        if not states:
            raise DebateError("all_models_failed",
                              "No model produced a valid answer: " + "; ".join(
                                  f"{ctx.aliases[m]}: {e}" for m, e in errors.items()))
        if not early:
            stop = self._check_stop(ctx, 0, states, changed=True)
            rnd = 0
            while stop is None:
                rnd += 1
                changed = await self._round(ctx, rnd, states, rounds, errors, flips, None)
                stop = self._check_stop(ctx, rnd, states, changed=changed)
            emit("stopped", stop)

        tools = await self._ground(ctx) if ctx.ground else None
        tally, ruling, final = await self.adjudicate(ctx, states, skip_judge=early, tools=tools)
        emit("tally", tally)
        emit("judge_ruling", ruling)
        emit("final", final)
        return DebateResult(run_id, ctx.mode, ctx.aliases, final, tally, ruling, stop, rounds,
                            flips, errors, states=states, ctx=ctx)

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

    async def adjudicate(self, ctx: Ctx, states: dict[str, State], *, skip_judge: bool = False,
                         tools: list[ToolFinding] | None = None) -> tuple[Tally, JudgeRuling, Final]:
        tally = build_tally(
            mode=ctx.mode, pack_id=ctx.pack.id, pack_categories=ctx.pack.categories,
            outputs={m: s.out for m, s in states.items()},
            findings={m: s.findings for m, s in states.items()}, aliases=ctx.aliases,
            weights=ctx.weights, category_hint=ctx.category,
            threshold=self.settings.vote_threshold, tol=self.settings.line_tolerance,
            tool_findings=tools, by_category=self.settings.cluster_by_category)
        ruling = (self._skipped_ruling(ctx, tally) if skip_judge
                  else await self._judge(ctx, tally))
        return tally, ruling, self._finalize(ctx, tally, ruling, states)

    def _skipped_ruling(self, ctx: Ctx, tally: Tally) -> JudgeRuling:
        return JudgeRuling(
            judge_model_id=ctx.judge_id, skipped=True, overrides_vote=False,
            rulings=[ClusterRuling(cluster_id=c.id, accept=c.vote_accept, vote_accept=c.vote_accept,
                                   overrides_vote=False) for c in tally.clusters],
            reason="Skipped: the first-pass models agreed with high confidence.",
            injection_flags=ctx.flags)

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
            **({"tool_verified": c.tool_verified} if ctx.ground else {}),
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
            if c.tool_verified:  # a tool outranks the judge too
                rulings.append(ClusterRuling(cluster_id=c.id, accept=True, vote_accept=True,
                                             overrides_vote=False,
                                             reason=f"Confirmed by {c.tool_rule}." if c.tool_rule else ""))
                continue
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
            reason=res.output.reason if over else "", injection_flags=ctx.flags,
            recommendation=res.output.recommendation.strip())

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
                   f"{len(dissent)} dissenting; "
                   + ("the judge was skipped (early finish)." if ruling.skipped else
                      f"the judge overrode the weighted vote on {n_over} cluster(s)." if n_over
                      else "the judge agreed with the weighted vote."))
        agreements, clashes = self._agreements_and_clashes(ctx, tally, states)
        return Final(run_id=ctx.run_id, verdict=FinalVerdict(overall=overall, summary=summary),
                     confirmed_findings=confirmed, disputed_findings=disputed, dissent=dissent,
                     agreements=agreements, clashes=clashes,
                     blind_spots=self._blind_spots(ctx, states),
                     recommendation=self._recommendation(ctx, ruling, overall, confirmed, states),
                     next_step=self._next_step(ctx, confirmed), usage=ctx.usage())

    # ------------------------------------------------------------------ v1.1 final sections

    def _agreements_and_clashes(self, ctx: Ctx, tally: Tally, states: dict[str, State]
                                ) -> tuple[list[Agreement], list[Clash]]:
        everyone = [m for m in ctx.models if m in states]
        agreements: list[Agreement] = []
        clashes: list[Clash] = []
        for c in tally.clusters:
            backers = [s.model_id for s in c.supporters]
            others = [m for m in everyone if m not in backers]
            if not others and len(everyone) >= 2:
                agreements.append(Agreement(cluster_id=c.id, claim=c.claim,
                                            aliases=[ctx.aliases[m] for m in backers]))
            elif others and backers:
                opp = max(others, key=lambda m: states[m].out.position.confidence)
                clashes.append(Clash(
                    cluster_id=c.id, claim=c.claim,
                    for_side=ClashSide(aliases=[ctx.aliases[m] for m in backers],
                                       argument=(c.evidence or c.claim)[:400]),
                    against_side=ClashSide(aliases=[ctx.aliases[m] for m in others],
                                           argument=states[opp].out.text[:400])))
        if not tally.clusters and len(everyone) >= 2 and len(
                {states[m].out.position.verdict for m in everyone}) == 1:
            verdict = states[everyone[0]].out.position.verdict
            agreements.append(Agreement(
                cluster_id=None, aliases=[ctx.aliases[m] for m in everyone],
                claim="Nothing to flag." if verdict == "no_issue" else f"Verdict: {verdict}."))
        return agreements, clashes

    def _blind_spots(self, ctx: Ctx, states: dict[str, State]) -> list[BlindSpot]:
        out, seen = [], set()
        for m in ctx.models:
            crit = states[m].out.critique if m in states else None
            if not crit:
                continue
            for kind in ("biggest_blind_spot", "missed_by_all"):
                text = getattr(crit, kind).strip()
                key = text.lower().rstrip(".")
                if key in ("", "none", "n/a", "nothing", "no", "none."):
                    continue
                if key not in seen:
                    seen.add(key)
                    out.append(BlindSpot(alias=ctx.aliases[m], kind=kind, text=text))
        return out

    def _recommendation(self, ctx: Ctx, ruling: JudgeRuling, overall: str, confirmed: list,
                        states: dict[str, State]) -> Recommendation:
        w = {m: ctx.weights.weight(m, ctx.pack.id, ctx.category)[0] for m in states}
        total = sum(w.values())
        agree = sum(w[m] * s.out.position.confidence for m, s in states.items()
                    if s.out.position.verdict == overall)
        conf = min(1.0, agree / total) if total else 0.0
        if ruling.recommendation:
            text = ruling.recommendation
        elif ctx.mode == "claim":
            text = ("The panel could not settle this claim." if overall == "unsure"
                    else f"The panel judges the claim {overall}.")
        elif confirmed:
            top = max(confirmed, key=lambda c: (c.tool_verified, c.weighted_score))
            text = (f"Fix {len(confirmed)} confirmed issue(s), starting with lines "
                    f"{top.line_start}-{top.line_end} ({top.category}): {top.claim}")
        else:
            text = "No defect was confirmed; the change looks fine to merge."
        return Recommendation(verdict=overall, text=text, confidence=conf,  # type: ignore[arg-type]
                              tool_verified=any(c.tool_verified for c in confirmed))

    def _next_step(self, ctx: Ctx, confirmed: list) -> NextStep | None:
        if ctx.mode != "review" or not confirmed:
            return None
        top = max(confirmed, key=lambda c: (c.tool_verified, c.weighted_score))
        return NextStep(
            cluster_id=top.id, patch=top.fix or None,
            text=f"Fix lines {top.line_start}-{top.line_end} ({top.category}): {top.claim}")
