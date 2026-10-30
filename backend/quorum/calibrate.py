"""Calibration (B7): measure each model per category on a pack's calibration corpus."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from .cluster import spans_overlap
from .engine import Debate, State
from .schemas import ClaimLabel, CorpusItem, ReviewLabel

Progress = Callable[[int, int, str], None]


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def is_positive(item: CorpusItem) -> bool:
    """Positive = there is something to catch: a bug, or a false claim."""
    lab = item.label
    return lab.has_issue if isinstance(lab, ReviewLabel) else not lab.truth


def hits_label(item: CorpusItem, lo: int, hi: int, tol: int) -> bool:
    lab = item.label
    return isinstance(lab, ReviewLabel) and lab.lines is not None and spans_overlap(
        (lo, hi), lab.lines, tol)


def score_state(item: CorpusItem, state: State, tol: int) -> bool:
    """Is one blind answer right? A bug must be localised; a clean item must be called clean."""
    lab, v = item.label, state.out.position.verdict
    if isinstance(lab, ClaimLabel):
        return v == str(lab.truth).lower()
    if lab.has_issue:
        return v == "issue" and any(hits_label(item, f.line_start, f.line_end, tol)
                                    for f in state.findings)
    return v == "no_issue"


async def calibrate_model(
    debate: Debate, model_id: str, pack_id: str, *, limit: int | None = None,
    progress: Progress | None = None, split: str = "calibration",
) -> dict:
    """Run the corpus, write the model's profile cells, return a summary.

    Items where the model produced no answer at all (transport failure, unusable output after a
    repair attempt) are skipped rather than scored, and reported; if more than a third are
    skipped the profile is not written, because it would mislead.
    """
    pack = debate.packs.get(pack_id)
    items = debate.packs.corpus(pack_id, split)[:limit]
    mode = pack.modes[0]
    tol = debate.settings.line_tolerance
    counts: dict[str, list[int]] = {c: [0, 0] for c in pack.categories}
    skipped: list[str] = []
    for i, item in enumerate(items, 1):
        state, res = await debate.blind_one(pack, mode, item.input, model_id)
        if state is None:
            skipped.append(f"{item.id}: {res.error}")
        else:
            cell = counts.setdefault(item.category, [0, 0])
            cell[0] += 1
            cell[1] += int(score_state(item, state, tol))
        if progress:
            progress(i, len(items), f"{model_id} · {item.id}")
    if len(skipped) * 3 > len(items):
        raise CalibrationError(
            f"{len(skipped)} of {len(items)} items got no usable answer from {model_id}; "
            f"first problem: {skipped[0]}")
    store = debate.store
    when = now()
    store.clear_profile(model_id, pack_id)
    for cat, (n, correct) in counts.items():
        if n:
            store.set_profile_cell(model_id, pack_id, cat, n, correct, when)
    return {"model_id": model_id, "pack": pack_id, "items": len(items), "skipped": skipped,
            "cells": {c: {"n": n, "correct": k} for c, (n, k) in counts.items() if n}}


class CalibrationError(RuntimeError):
    pass


async def calibrate_many(
    debate: Debate, model_ids: list[str], pack_ids: list[str], *, limit: int | None = None,
    progress: Progress | None = None,
) -> dict:
    total = sum(len(debate.packs.corpus(p, "calibration")[:limit]) for p in pack_ids) * len(model_ids)
    done = 0
    results, failures = [], []
    for m in model_ids:
        debate.store.set_model_status(m, "calibrating", "")
        ok = True
        for p in pack_ids:
            base = done

            def bump(i: int, _n: int, msg: str, base=base):
                if progress:
                    progress(base + i, total, msg)

            try:
                results.append(await calibrate_model(debate, m, p, limit=limit, progress=bump))
            except Exception as e:  # noqa: BLE001 - reported per model, other models continue
                ok = False
                failures.append({"model_id": m, "pack": p, "error": str(e)})
                debate.store.set_model_status(m, "error", str(e)[:300])
            done += len(debate.packs.corpus(p, "calibration")[:limit])
        if ok:
            debate.store.set_model_status(m, "ready", "")
    return {"calibrated": results, "failures": failures}
