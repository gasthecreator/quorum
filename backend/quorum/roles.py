"""Role routing (B16): give each panelist an angle that matches what it measured best at.

Roles are one or two sentences on purpose: long persona prompts are fragile on 3 to 4B models, and a
persona is not evidence. Whether roles help is an empirical question, so the evaluation reports a
roles-on condition next to the roles-off baseline.
"""

from __future__ import annotations

import re

from .schemas import Pack, RoleAssignment
from .weights import WeightBook

ROLE_ORDER = ["attacker", "maintainer", "operator"]

ROLE_TEXT = {
    "attacker": ("Your angle: you are the attacker. Look first for ways untrusted input could be "
                 "abused (injection, bypass, leaks). Still report any other defect you are sure of, "
                 "and never invent one to fit your role."),
    "maintainer": ("Your angle: you are the maintainer. Look first for logic and boundary mistakes "
                   "(off-by-one, missing awaits, wrong conditions). Still report any other defect "
                   "you are sure of, and never invent one to fit your role."),
    "operator": ("Your angle: you are the on-call operator. Look first for what breaks under load "
                 "(unbounded queries, missing limits or timeouts, leaks). Still report any other "
                 "defect you are sure of, and never invent one to fit your role."),
    "outsider": ("You have only the code in front of you, with no hints from anyone. Review it "
                 "cold and report only what you can point to."),
}

_KEYWORDS: list[tuple[str, re.Pattern]] = [
    ("attacker", re.compile(r"inject|xss|csrf|auth|secret|crypt|travers|ssrf|deserial|sanitiz|escape|taint|permission", re.IGNORECASE)),
    ("operator", re.compile(r"unbound|limit|timeout|leak|(?<![a-z])n[\W_]*(?:\+|plus)[\W_]*(?:1|one)|perf|slow|resource|memory|pagina|retry", re.IGNORECASE)),
    ("maintainer", re.compile(r"off.?by|await|async|race|null|none|bound|logic|type|condition|edge|index|overflow", re.IGNORECASE)),
]


def role_for_category(category: str, overrides: dict[str, str] | None = None) -> str | None:
    """Which role owns a category. A pack can pin it with `category_roles`; otherwise by keyword."""
    if overrides and overrides.get(category) in ROLE_TEXT:
        return overrides[category]
    for role, pat in _KEYWORDS:
        if pat.search(category):
            return role
    return None


def _score(weights: WeightBook, model_id: str, pack: Pack, role: str) -> tuple[float, bool]:
    """Pooled Wilson lower bound over the categories the role owns; (score, measured?)."""
    from .stats import wilson_lower

    n = correct = 0
    for cat in pack.categories:
        if role_for_category(cat, pack.category_roles) != role:
            continue
        nc = weights._cells.get((model_id, pack.id, cat))
        if nc and nc[0] > 0:
            n, correct = n + nc[0], correct + nc[1]
    if n:
        return wilson_lower(correct, n), True
    return weights.default, False


def assign_roles(models: list[str], pack: Pack, weights: WeightBook, *,
                 outsider: bool = False) -> dict[str, RoleAssignment]:
    """Greedy best-fit: repeatedly give the strongest remaining (model, role) pair its role.

    `models` is in alias order, which breaks ties, so the result is deterministic. Roles nobody can
    fill are dropped; models left over get no role. With outsider=True (3+ models) the weakest
    specialist is taken out first and told to review cold.
    """
    roles = [r for r in ROLE_ORDER if any(
        role_for_category(c, pack.category_roles) == r for c in pack.categories)]
    scores = {(m, r): _score(weights, m, pack, r) for m in models for r in roles}
    out: dict[str, RoleAssignment] = {}
    pool = list(models)
    if outsider and len(models) >= 3:
        def best(m: str) -> float:
            return max((scores[(m, r)][0] for r in roles), default=0.0)

        pick = min(reversed(pool), key=best)  # reversed: ties go to the last alias
        out[pick] = RoleAssignment(role="outsider", source="chosen",
                                   detail="Weakest specialist; sees only the input and never revises.")
        pool.remove(pick)
    free_roles = list(roles)
    while pool and free_roles:
        m, r = max(((m, r) for m in pool for r in free_roles),
                   key=lambda mr: (scores[mr][0], -models.index(mr[0]), -ROLE_ORDER.index(mr[1])))
        score, measured = scores[(m, r)]
        out[m] = RoleAssignment(
            role=r, source="measured" if measured else "default",
            detail=(f"Best measured fit for {r} categories (Wilson lower bound {score:.2f})."
                    if measured else "No calibration for these categories yet; assigned by order."))
        pool.remove(m)
        free_roles.remove(r)
    return out
