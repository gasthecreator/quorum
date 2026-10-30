import pytest

from quorum.calibrate import calibrate_many, calibrate_model
from quorum.evaluate import run_eval
from quorum.packs import PackError
from quorum.schemas import EvalRequest
from quorum.stats import weight_from_lower, wilson_interval, wilson_lower
from quorum.weights import WeightBook

from .conftest import ids


def test_wilson_known_values():
    lo, hi = wilson_interval(8, 10)
    assert lo == pytest.approx(0.4902, abs=1e-3) and hi == pytest.approx(0.9433, abs=1e-3)
    assert wilson_lower(0, 0) == 0.0
    assert wilson_interval(0, 0) == (0.0, 1.0)
    assert weight_from_lower(0.0) == 0.05


async def test_calibration_writes_profiles(debate, registry, store):
    m = ids(registry)
    out = await calibrate_model(debate, m["sharp"], "code-review-dev")
    assert out["skipped"] == []
    book = WeightBook(store.profile_cells())
    cell = book.cell(m["sharp"], "code-review-dev", "sql_injection")
    assert cell.n == 3 and cell.correct == 3  # bug, clean control and the injection probe
    assert cell.lower == pytest.approx(wilson_lower(3, 3))
    assert cell.weight == pytest.approx(cell.lower)


async def test_profiles_differ_between_models(debate, registry, store):
    m = ids(registry)
    await calibrate_many(debate, [m["sharp"], m["sloppy"]], ["code-review-dev"])
    book = WeightBook(store.profile_cells())
    sharp = book.weight(m["sharp"], "code-review-dev", None)[0]
    sloppy = book.weight(m["sloppy"], "code-review-dev", None)[0]
    assert sharp > sloppy
    assert book.weight(m["sloppy"], "code-review-dev", "unbounded_query")[1] == "measured"
    assert book.weight(m["stubborn"], "code-review-dev", "off_by_one")[1] == "default"
    assert store.get_model(m["sharp"])["status"] == "ready"


async def test_calibration_refuses_when_model_broken(debate, registry, store):
    m = ids(registry)
    out = await calibrate_many(debate, [m["broken"]], ["code-review-dev"])
    assert out["failures"] and store.get_model(m["broken"])["status"] == "error"
    assert store.profile_cells() == []


async def test_eval_produces_six_conditions(debate, registry):
    m = ids(registry)
    models = [m["sharp"], m["sloppy"], m["stubborn"]]
    await calibrate_many(debate, models, ["code-review-dev"])
    res = await run_eval(debate, EvalRequest(pack="code-review-dev", model_ids=models,
                                             judge_model_id=m["judge-skeptic"]))
    assert [c.id for c in res.conditions] == [1, 2, 3, 4, 5, 6]
    assert res.conditions[5].available is False
    assert res.n_items == 8 and res.n_positive == 4 and res.n_negative == 4
    for c in res.conditions[:5]:
        assert c.tp + c.fp + c.tn + c.fn == 8
        assert c.f1.lo <= c.f1.value <= c.f1.hi
    sharp_alone = res.conditions[0]
    assert sharp_alone.recall.value == 1.0  # the best single model finds all four seeded bugs
    assert (debate.settings.home / "eval" / f"{res.id}.csv").is_file()
    assert debate.store.latest_eval("code-review-dev")["id"] == res.id


async def test_eval_rejects_contaminated_heldout(debate, registry, tmp_path, settings):
    import shutil
    from pathlib import Path

    from quorum.packs import PackStore
    src = Path(settings.pack_dirs[0]) / "code-review-dev"
    dst = tmp_path / "packs" / "code-review-dev"
    shutil.copytree(src, dst)
    (dst / "heldout.jsonl").write_text((dst / "calibration.jsonl").read_text().splitlines()[0] + "\n")
    debate.packs = PackStore((tmp_path / "packs",))
    with pytest.raises(PackError):
        await run_eval(debate, EvalRequest(pack="code-review-dev", model_ids=[ids(registry)["sharp"]]))


def test_dev_packs_are_valid(packs):
    for p in packs.list():
        assert packs.validate(p.id) == [], p.id
