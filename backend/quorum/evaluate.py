"""Held-out evaluation (B8): six conditions on items no model was calibrated on."""

from __future__ import annotations

import csv
import json
import uuid
from collections.abc import Callable
from pathlib import Path

from .calibrate import hits_label, is_positive, now, score_state
from .cluster import build_tally
from .engine import Debate, State, noop_emit
from .packs import PackError
from .schemas import (
    ClaimLabel,
    Cluster,
    ConditionResult,
    CorpusItem,
    DebateMovement,
    EvalRequest,
    EvalResult,
    Interval,
    RunRequest,
)
from .stats import bootstrap_interval, wilson_interval
from .weights import FixedWeights, WeightBook

CONDITION_NAMES = {
    1: "Best single model",
    2: "Majority vote (blind, equal weights)",
    3: "Weighted vote (blind, calibrated weights)",
    4: "Weighted vote + judge",
    5: "Debate + weighted vote + judge",
    6: "Condition 5 + tool grounding",
}

Progress = Callable[[int, int, str], None]


def _predicts_positive(item: CorpusItem, accepted: list[Cluster]) -> bool:
    if isinstance(item.label, ClaimLabel):
        return any(c.verdict == "false" for c in accepted)
    return bool(accepted)


def _outcome(item: CorpusItem, accepted: list[Cluster], tol: int) -> str:
    """tp / fp / tn / fn for one item. A bug only counts as caught if a cluster lands on it."""
    pos = is_positive(item)
    if not pos:
        return "fp" if _predicts_positive(item, accepted) else "tn"
    if isinstance(item.label, ClaimLabel):
        return "tp" if _predicts_positive(item, accepted) else "fn"
    hit = any(hits_label(item, c.line_start, c.line_end, tol) for c in accepted)
    return "tp" if hit else "fn"


def _f1(outcomes) -> float:
    tp, fp, fn = (sum(1 for o in outcomes if o == k) for k in ("tp", "fp", "fn"))
    return 2 * tp / (2 * tp + fp + fn) if (2 * tp + fp + fn) else 0.0


def _iv(k: int, n: int) -> Interval | None:
    if n == 0:
        return None
    lo, hi = wilson_interval(k, n)
    return Interval(value=k / n, lo=lo, hi=hi)


def summarize(cid: int, outcomes: list[str]) -> ConditionResult:
    c = {k: outcomes.count(k) for k in ("tp", "fp", "tn", "fn")}
    f1, lo, hi = bootstrap_interval(outcomes, _f1, seed=cid)
    return ConditionResult(
        id=cid, name=CONDITION_NAMES[cid], available=True, n_items=len(outcomes), **c,
        precision=_iv(c["tp"], c["tp"] + c["fp"]), recall=_iv(c["tp"], c["tp"] + c["fn"]),
        f1=Interval(value=f1, lo=lo, hi=hi) if outcomes else None,
        fpr=_iv(c["fp"], c["fp"] + c["tn"]),
        accuracy=_iv(c["tp"] + c["tn"], len(outcomes)))


async def run_eval(debate: Debate, req: EvalRequest, progress: Progress | None = None) -> EvalResult:
    packs, store, settings = debate.packs, debate.store, debate.settings
    problems = [p for p in packs.validate(req.pack) if "overlaps" in p]
    if problems:
        raise PackError("held-out split is contaminated: " + "; ".join(problems[:3]))
    pack = packs.get(req.pack)
    mode = pack.modes[0]
    items = packs.corpus(req.pack, "heldout")[:req.limit]
    if not items:
        raise PackError(f"pack '{req.pack}' has no held-out items")
    tol = settings.line_tolerance
    book = WeightBook(store.profile_cells(), settings.default_weight)
    flat = FixedWeights(1.0)
    debate.registry.require(req.model_ids)
    judge = req.judge_model_id or debate.registry.default_judge() or req.model_ids[0]
    notes: list[str] = []

    pooled = {m: book.weight(m, req.pack, None) for m in req.model_ids}
    uncal = [m for m, (_, src) in pooled.items() if src == "default"]
    if uncal:
        notes.append("Not calibrated on this pack, so default weights were used: " + ", ".join(uncal))
    best = max(req.model_ids, key=lambda m: pooled[m][0])
    if len({w for w, _ in pooled.values()}) == 1:
        notes.append("All models have the same pooled weight, so 'best single model' is arbitrary.")

    outcomes: dict[int, list[str]] = {i: [] for i in range(1, 6)}
    toward_right = toward_wrong = neutral = 0
    total_steps = len(items)

    for n, item in enumerate(items, 1):
        base = RunRequest(pack=req.pack, input=item.input, model_ids=req.model_ids,
                          judge_model_id=judge, max_rounds=req.max_rounds, seed=n)
        ctx = debate.context(base, f"eval-{n}", noop_emit, weights=book)
        blind: dict = {}
        states: dict[str, State] = {}
        for m in ctx.models:
            st, res = await debate.blind_one(pack, mode, item.input, m, ctx.nonce)
            blind[m] = res
            if st:
                states[m] = st
        if not states:
            notes.append(f"{item.id}: no model answered; item skipped")
            continue

        def tally_for(weights, states=states, ctx=ctx):
            return build_tally(
                mode=mode, pack_id=req.pack, pack_categories=pack.categories,
                outputs={m: s.out for m, s in states.items()},
                findings={m: s.findings for m, s in states.items()}, aliases=ctx.aliases,
                weights=weights, category_hint=None, threshold=settings.vote_threshold, tol=tol)

        # 1: best calibrated model alone
        if best in states:
            t1 = build_tally(
                mode=mode, pack_id=req.pack, pack_categories=pack.categories,
                outputs={best: states[best].out}, findings={best: states[best].findings},
                aliases=ctx.aliases, weights=flat, category_hint=None,
                threshold=settings.vote_threshold, tol=tol)
            outcomes[1].append(_outcome(item, [c for c in t1.clusters if c.vote_accept], tol))
        else:
            outcomes[1].append(_outcome(item, [], tol))
        # 2 and 3: votes on the blind round
        outcomes[2].append(_outcome(item, [c for c in tally_for(flat).clusters if c.vote_accept], tol))
        tally3 = tally_for(book)
        outcomes[3].append(_outcome(item, [c for c in tally3.clusters if c.vote_accept], tol))
        # 4: add the judge
        ctx.weights = book
        _, _, final4 = await debate.adjudicate(ctx, states)
        outcomes[4].append(_outcome(item, final4.confirmed_findings, tol))
        # 5: full debate, reusing the same blind answers
        result = await debate.run(base, f"eval-{n}", noop_emit, blind=blind, weights=book)
        outcomes[5].append(_outcome(item, result.final.confirmed_findings if result.final else [], tol))
        # did debate move models toward the truth?
        if len(result.rounds) > 1:
            last = {m: s for r in result.rounds for m, s in r.items()}
            for m, before in states.items():
                after = last.get(m)
                if after is None:
                    continue
                b, a = score_state(item, before, tol), score_state(item, after, tol)
                if b == a:
                    neutral += 1
                elif a:
                    toward_right += 1
                else:
                    toward_wrong += 1
        if progress:
            progress(n, total_steps, item.id)

    conditions = [summarize(i, outcomes[i]) for i in range(1, 6)]
    conditions.append(ConditionResult(
        id=6, name=CONDITION_NAMES[6], available=False,
        reason_unavailable="Tool grounding (stretch goal B10) is not built."))
    result = EvalResult(
        id=uuid.uuid4().hex[:10], created_at=now(), pack=req.pack, model_ids=req.model_ids,
        judge_model_id=judge, max_rounds=req.max_rounds, n_items=len(outcomes[1]),
        n_positive=sum(is_positive(i) for i in items), n_negative=sum(not is_positive(i) for i in items),
        conditions=conditions,
        debate_movement=DebateMovement(
            toward_correct=toward_right, toward_wrong=toward_wrong, neutral=neutral,
            note="Per (item, model): did a debate round change whether that model's answer was right?"),
        notes=notes + [
            "Weights come from the calibration split only; these items were never used for it.",
            "Intervals: Wilson 95% for proportions, bootstrap 95% for F1.",
        ])
    store.save_eval(result.id, result.pack, result.created_at, result.model_dump(mode="json"))
    export_eval(result, debate.settings.home / "eval")
    return result


def export_eval(result: EvalResult, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    jp, cp = out_dir / f"{result.id}.json", out_dir / f"{result.id}.csv"
    jp.write_text(json.dumps(result.model_dump(mode="json"), indent=2))
    with cp.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["condition", "name", "available", "n", "tp", "fp", "tn", "fn", "precision",
                    "precision_lo", "precision_hi", "recall", "recall_lo", "recall_hi", "f1", "f1_lo",
                    "f1_hi", "fpr", "fpr_lo", "fpr_hi"])
        for c in result.conditions:
            row = [c.id, c.name, c.available, c.n_items, c.tp, c.fp, c.tn, c.fn]
            for iv in (c.precision, c.recall, c.f1, c.fpr):
                row += [round(iv.value, 4), round(iv.lo, 4), round(iv.hi, 4)] if iv else ["", "", ""]
            w.writerow(row)
    return jp, cp
