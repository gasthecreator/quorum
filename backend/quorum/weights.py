"""Skill profiles turned into vote weights."""

from __future__ import annotations

from .schemas import ProfileCell, WeightSource
from .stats import weight_from_lower, wilson_lower


def make_cell(n: int, correct: int) -> ProfileCell:
    from .stats import wilson_lower as lower_fn

    lower = lower_fn(correct, n)
    return ProfileCell(n=n, correct=correct, rate=(correct / n if n else 0.0), lower=lower,
                       weight=weight_from_lower(lower) if n else 0.0)


class WeightBook:
    """Looks up (model, pack, category) weights.

    measured: the model has a calibrated cell for this category.
    pooled:   no cell for the category; use the model's pooled result over the whole pack.
    default:  the model has never been calibrated on this pack.
    """

    def __init__(self, cells: list[dict], default_weight: float = 0.5):
        self.default = default_weight
        self._cells: dict[tuple[str, str, str], tuple[int, int]] = {
            (c["model_id"], c["pack_id"], c["category"]): (c["n"], c["correct"]) for c in cells
        }

    def cell(self, model_id: str, pack_id: str, category: str) -> ProfileCell | None:
        nc = self._cells.get((model_id, pack_id, category))
        return make_cell(*nc) if nc and nc[0] > 0 else None

    def weight(self, model_id: str, pack_id: str, category: str | None) -> tuple[float, WeightSource]:
        if category:
            c = self.cell(model_id, pack_id, category)
            if c:
                return c.weight, "measured"
        n = correct = 0
        for (m, p, _), (cn, cc) in self._cells.items():
            if m == model_id and p == pack_id:
                n, correct = n + cn, correct + cc
        if n:
            return weight_from_lower(wilson_lower(correct, n)), "pooled"
        return self.default, "default"

    def profiles(self) -> dict[str, dict[str, dict[str, ProfileCell]]]:
        out: dict[str, dict[str, dict[str, ProfileCell]]] = {}
        for (m, p, cat), (n, correct) in sorted(self._cells.items()):
            out.setdefault(m, {}).setdefault(p, {})[cat] = make_cell(n, correct)
        return out


class FixedWeights(WeightBook):
    """Equal weights, for the majority-vote baseline."""

    def __init__(self, value: float = 1.0):
        super().__init__([], value)

    def weight(self, model_id, pack_id, category):
        return self.default, "default"
