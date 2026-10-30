"""B16: role routing from measured profiles, and the outsider."""

import pytest

from quorum.calibrate import calibrate_many
from quorum.evaluate import run_eval
from quorum.roles import ROLE_TEXT, assign_roles, role_for_category
from quorum.schemas import EvalRequest, RunRequest
from quorum.weights import WeightBook

from .conftest import ids

UNBOUNDED = ('def list_events(db, user_id):\n    rows = db.execute("SELECT * FROM events WHERE user_id = ?", (user_id,))\n'
             '    return [dict(r) for r in rows.fetchall()]\n')
ALL3 = ["sharp", "sloppy", "stubborn"]


def cells(rows):
    return WeightBook([{"model_id": m, "pack_id": "code-review-dev", "category": c, "n": n, "correct": k}
                       for m, c, n, k in rows])


def test_category_to_role_by_keyword_and_override():
    assert role_for_category("sql_injection") == "attacker"
    assert role_for_category("off_by_one") == "maintainer"
    assert role_for_category("missing_await") == "maintainer"
    assert role_for_category("unbounded_query") == "operator"
    assert role_for_category("n_plus_one") == "operator"
    assert role_for_category("naming_style") is None
    assert role_for_category("naming_style", {"naming_style": "maintainer"}) == "maintainer"
    assert role_for_category("sql_injection", {"sql_injection": "operator"}) == "operator"


def test_every_role_text_is_short(packs):
    for role, text in ROLE_TEXT.items():
        assert len(text.split(". ")) <= 3 and len(text) < 330, role  # one or two sentences, not a persona


def test_roles_go_to_the_best_measured_model(packs):
    pack = packs.get("code-review-dev")
    w = cells([
        ("A", "sql_injection", 10, 10), ("A", "off_by_one", 10, 4), ("A", "unbounded_query", 10, 4),
        ("B", "sql_injection", 10, 5), ("B", "off_by_one", 10, 10), ("B", "missing_await", 10, 10),
        ("B", "unbounded_query", 10, 5),
        ("C", "sql_injection", 10, 5), ("C", "off_by_one", 10, 5), ("C", "unbounded_query", 10, 10),
    ])
    got = assign_roles(["A", "B", "C"], pack, w)
    assert {m: r.role for m, r in got.items()} == {"A": "attacker", "B": "maintainer", "C": "operator"}
    assert all(r.source == "measured" for r in got.values())
    assert "Wilson" in got["A"].detail


def test_each_model_gets_at_most_one_role_even_if_one_model_is_best_at_everything(packs):
    pack = packs.get("code-review-dev")
    w = cells([("A", c, 10, 10) for c in pack.categories] + [("B", c, 10, 6) for c in pack.categories]
              + [("C", c, 10, 3) for c in pack.categories])
    got = assign_roles(["A", "B", "C"], pack, w)
    assert sorted(r.role for r in got.values()) == ["attacker", "maintainer", "operator"]
    assert len(got) == 3


def test_uncalibrated_models_get_default_assignment_in_alias_order(packs):
    got = assign_roles(["A", "B", "C"], packs.get("code-review-dev"), WeightBook([]))
    assert [got[m].role for m in "ABC"] == ["attacker", "maintainer", "operator"]
    assert all(r.source == "default" for r in got.values())


def test_fewer_models_than_roles_and_more_models_than_roles(packs):
    pack = packs.get("code-review-dev")
    assert len(assign_roles(["A", "B"], pack, WeightBook([]))) == 2
    assert len(assign_roles(list("ABCDE"), pack, WeightBook([]))) == 3  # the rest get no role


def test_outsider_is_the_weakest_specialist(packs):
    pack = packs.get("code-review-dev")
    w = cells([("A", c, 10, 10) for c in pack.categories] + [("B", c, 10, 7) for c in pack.categories]
              + [("C", c, 10, 2) for c in pack.categories])
    got = assign_roles(["A", "B", "C"], pack, w, outsider=True)
    assert got["C"].role == "outsider" and got["C"].source == "chosen"
    assert {got["A"].role, got["B"].role} <= {"attacker", "maintainer", "operator"}
    assert assign_roles(["A", "B"], pack, w, outsider=True).get("B", None) is None or \
        all(r.role != "outsider" for r in assign_roles(["A", "B"], pack, w, outsider=True).values())


async def go(debate, registry, text, models=ALL3, pack="code-review-dev", **kw):
    m = ids(registry)
    events = []
    req = RunRequest(pack=pack, input=text, model_ids=[m[x] for x in models], seed=3, **kw)
    res = await debate.run(req, "r1", lambda e, d: events.append((e, d)))
    return res, events, m


async def test_roles_reach_the_prompt_and_the_events(debate, registry):
    m = ids(registry)
    seen = {}
    for name in ALL3:
        p = registry.provider(m[name])
        orig = p.chat

        async def spy(messages, _o=orig, _n=name, **kw):
            seen.setdefault(_n, []).append(messages[0]["content"])
            return await _o(messages, **kw)

        p.chat = spy
    await calibrate_many(debate, [m[x] for x in ALL3], ["code-review-dev"])
    seen.clear()
    res, events, _ = await go(debate, registry, UNBOUNDED, roles="profile")
    started = events[0][1]
    assert set(started.roles) == {m[x] for x in ALL3}
    for mid, a in started.roles.items():
        name = next(n for n in ALL3 if m[n] == mid)
        debater = [x for x in seen[name] if "You are the judge" not in x and "restate" not in x]
        assert debater and all(ROLE_TEXT[a.role] in x for x in debater), name
    msgs = [d for e, d in events if e == "model_message"]
    assert all(d.role == started.roles[d.model_id].role for d in msgs)
    judge_prompts = [s for n in ALL3 for s in seen[n] if "You are the judge" in s]
    assert all("Your angle" not in s for s in judge_prompts)  # the judge is never given a role


async def test_roles_off_by_default_and_ignored_for_claims(debate, registry):
    _, events, _ = await go(debate, registry, UNBOUNDED)
    assert events[0][1].roles == {} and all(d.role is None for e, d in events if e == "model_message")
    _, events, _ = await go(debate, registry, "The Berlin Wall fell in 1989.",
                            pack="claim-check-dev", roles="profile", outsider=True)
    assert events[0][1].roles == {}


async def test_outsider_sees_no_framing_and_sits_out_visible_rounds(debate, registry):
    m = ids(registry)
    calls = {}
    for name in ALL3:
        p = registry.provider(m[name])
        orig = p.chat

        async def spy(messages, _o=orig, _n=name, **kw):
            calls.setdefault(_n, []).append(messages[-1]["content"])
            return await _o(messages, **kw)

        p.chat = spy
    res, events, _ = await go(debate, registry, UNBOUNDED, outsider=True)
    started = events[0][1]
    (out_id,) = [mid for mid, a in started.roles.items() if a.role == "outsider"]
    out_name = next(n for n in ALL3 if m[n] == out_id)
    assert len(started.roles) == 1
    seen = calls[out_name]
    assert seen and all("=== FRAMING" not in s and "=== PEER" not in s for s in seen)
    msgs = [d for e, d in events if e == "model_message" and d.model_id == out_id]
    assert [d.round for d in msgs] == [0] and msgs[0].role == "outsider"
    others = [d for e, d in events if e == "model_message" and d.model_id != out_id]
    assert max(d.round for d in others) >= 1  # the debate itself still ran
    assert res.final.verdict.overall == "issue"


async def test_outsider_ignored_with_two_models(debate, registry):
    _, events, _ = await go(debate, registry, UNBOUNDED, models=["sharp", "sloppy"], outsider=True)
    assert events[0][1].roles == {}


async def test_eval_condition_seven_is_an_ablation_of_five(debate, registry):
    m = ids(registry)
    models = [m[x] for x in ALL3]
    debate.grounder = None
    await calibrate_many(debate, models, ["code-review-dev"])
    res = await run_eval(debate, EvalRequest(pack="code-review-dev", model_ids=models,
                                             judge_model_id=m["sharp"], roles=True))
    c7 = res.conditions[-1]
    assert c7.id == 7 and c7.available and c7.tp + c7.fp + c7.tn + c7.fn == 8
    assert any("ablation" in n for n in res.notes)
    res = await run_eval(debate, EvalRequest(pack="code-review-dev", model_ids=models,
                                             judge_model_id=m["sharp"]))
    assert [c.id for c in res.conditions] == [1, 2, 3, 4, 5, 6]


@pytest.mark.parametrize("bad", ["boss", ""])
def test_pack_cannot_pin_unknown_role(bad):
    from pydantic import ValidationError

    from quorum.schemas import Pack
    with pytest.raises(ValidationError):
        Pack(id="x", name="x", categories=["a"], modes=["review"], corpus={"calibration": "c"},
             category_roles={"a": bad})
