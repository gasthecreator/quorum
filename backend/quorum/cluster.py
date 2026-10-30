"""Clustering and tally (B5): group findings across models, then score clusters by weighted vote."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from .schemas import Cluster, DebaterOutput, Finding, Supporter, Tally, ToolFinding
from .weights import WeightBook


def spans_overlap(a: tuple[int, int], b: tuple[int, int], tol: int) -> bool:
    return a[0] - tol <= b[1] and b[0] - tol <= a[1]


def same_file(a: str, b: str) -> bool:
    return not a or not b or a == b


def finding_match(a: Finding, b: Finding, tol: int) -> bool:
    return same_file(a.file, b.file) and spans_overlap(
        (a.line_start, a.line_end), (b.line_start, b.line_end), tol)


@dataclass
class _Group:
    file: str
    start: int
    end: int
    members: dict[str, Finding]  # model_id -> finding

    category: str = ""

    def accepts(self, f: Finding, tol: int, by_category: bool = False) -> bool:
        if by_category and f.category != self.category:
            return False
        return same_file(self.file, f.file) and spans_overlap(
            (self.start, self.end), (f.line_start, f.line_end), tol)


def group_findings(per_model: dict[str, list[Finding]], tol: int,
                   by_category: bool = False) -> list[_Group]:
    """Group findings into clusters: same file, overlapping lines and, when by_category, the same
    category. Without the category, one model's wide or stray finding can swallow others' (seen with
    qwen2.5:3b), which reads as agreement that is not there."""
    flat = sorted(
        ((m, f) for m, fs in per_model.items() for f in fs),
        key=lambda x: (x[1].file, x[1].line_start, x[1].line_end, x[0]),
    )
    groups: list[_Group] = []
    for model_id, f in flat:
        target = next((g for g in groups if g.accepts(f, tol, by_category)), None)
        if target is None:
            groups.append(_Group(f.file, f.line_start, f.line_end, {model_id: f}, f.category))
        elif model_id not in target.members:
            target.members[model_id] = f
            target.start, target.end = min(target.start, f.line_start), max(target.end, f.line_end)
            target.file = target.file or f.file
    return groups


def _plurality_category(g: _Group, pack_categories: list[str],
                        outputs: dict[str, DebaterOutput]) -> str:
    """Most common in-pack category among members; ties go to the most confident member."""
    valid = {m: f for m, f in g.members.items() if f.category in pack_categories}
    pool = valid or g.members
    counts = Counter(f.category for f in pool.values())
    top = max(counts.values())
    tied = {c for c, n in counts.items() if n == top}
    best = max((m for m, f in pool.items() if f.category in tied),
               key=lambda m: outputs[m].position.confidence)
    return pool[best].category


def build_tally(
    *,
    mode: str,
    pack_id: str,
    pack_categories: list[str],
    outputs: dict[str, DebaterOutput],
    findings: dict[str, list[Finding]],
    aliases: dict[str, str],
    weights: WeightBook,
    category_hint: str | None,
    threshold: float,
    tol: int,
    tool_findings: list[ToolFinding] | None = None,
    by_category: bool = True,
) -> Tally:
    answering = list(outputs)

    def w(model_id: str, cat: str | None):
        return weights.weight(model_id, pack_id, cat)

    clusters: list[Cluster] = []
    if mode == "claim":
        total = sum(w(m, category_hint)[0] for m in answering)
        scored: list[Cluster] = []
        for verdict in ("true", "false"):
            sup = []
            for m in answering:
                pos = outputs[m].position
                if pos.verdict == verdict:
                    wt, src = w(m, category_hint)
                    sup.append(Supporter(model_id=m, alias=aliases[m], confidence=pos.confidence,
                                         weight=wt, weight_source=src))
            if not sup:
                continue
            score = sum(s.weight * s.confidence for s in sup)
            best = max(sup, key=lambda s: s.weight)
            scored.append(Cluster(
                id=f"v-{verdict}", kind="verdict", verdict=verdict, category=category_hint or "",
                claim=f"The claim is {verdict}.", evidence=outputs[best.model_id].text[:600],
                supporters=sup, weighted_score=score, total_weight=total,
                support_share=min(1.0, sum(s.weight for s in sup) / total) if total else 0.0,
                vote_accept=False))
        if scored:
            top = max(scored, key=lambda c: c.weighted_score)
            rivals = [c for c in scored if c is not top]
            win = all(top.weighted_score > c.weighted_score for c in rivals)
            for c in scored:
                c.vote_accept = c is top and win
        clusters = sorted(scored, key=lambda c: -c.weighted_score)
    else:
        for i, g in enumerate(group_findings(findings, tol, by_category), 1):
            category = _plurality_category(g, pack_categories, outputs)
            total = sum(w(m, category)[0] for m in answering)
            sup = []
            for m, f in g.members.items():
                wt, src = w(m, category)
                sup.append(Supporter(model_id=m, alias=aliases[m],
                                     confidence=outputs[m].position.confidence, weight=wt,
                                     weight_source=src, finding_id=f.id))
            best = max(sup, key=lambda s: s.weight * s.confidence)
            bf = g.members[best.model_id]
            fix = next((g.members[x.model_id].fix for x in sorted(
                sup, key=lambda z: -z.weight * z.confidence) if g.members[x.model_id].fix), "")
            share = min(1.0, sum(s.weight for s in sup) / total) if total else 0.0
            clusters.append(Cluster(
                id=f"k{i}", kind="finding", category=category, file=g.file, line_start=g.start,
                line_end=g.end, severity=max((f.severity for f in g.members.values()),
                                             key=["low", "medium", "high", "critical"].index),
                claim=bf.claim, evidence=bf.evidence, supporters=sup, fix=fix,
                weighted_score=sum(s.weight * s.confidence for s in sup), total_weight=total,
                support_share=share, vote_accept=share > threshold,
                tool_verified=any(f.tool_verified for f in g.members.values())))
        _apply_tools(clusters, tool_findings or [], answering, pack_id, weights, tol)
        clusters.sort(key=lambda c: (-c.weighted_score, c.line_start))
    return Tally(clusters=clusters, threshold=threshold)


def _apply_tools(clusters: list[Cluster], tools: list[ToolFinding], answering: list[str],
                 pack_id: str, weights: WeightBook, tol: int) -> None:
    """A tool-confirmed finding is accepted whatever the vote said. If no model raised it, it still
    becomes a cluster, with no supporters."""
    n_tool = 0
    for tf in tools:
        hit = next((c for c in clusters if c.kind == "finding" and spans_overlap(
            (c.line_start, c.line_end), (tf.line_start, tf.line_end), tol)), None)
        if hit is not None:
            hit.tool_verified, hit.vote_accept = True, True
            hit.tool_rule = hit.tool_rule or tf.rule_id
            continue
        n_tool += 1
        clusters.append(Cluster(
            id=f"t{n_tool}", kind="finding", category=tf.category, file=tf.file,
            line_start=tf.line_start, line_end=tf.line_end, severity="high", claim=tf.claim,
            evidence=f"{tf.tool} rule {tf.rule_id}", supporters=[], weighted_score=0.0,
            total_weight=sum(weights.weight(m, pack_id, tf.category)[0] for m in answering),
            support_share=0.0, vote_accept=True, tool_verified=True, tool_rule=tf.rule_id))
