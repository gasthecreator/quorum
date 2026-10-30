"""B10: tool grounding. These run the real Semgrep when it is installed and skip otherwise."""

import json

import pytest

from quorum.calibrate import calibrate_many
from quorum.evaluate import run_eval
from quorum.grounding import SemgrepGrounder, to_source
from quorum.schemas import EvalRequest, RunRequest, ToolFinding

from .conftest import ids

needs_semgrep = pytest.mark.skipif(not SemgrepGrounder().available(), reason="semgrep not installed")
ALL3 = ["sharp", "sloppy", "stubborn"]
LAST = "def last(items):\n    return items[len(items)]\n"


def test_diff_is_converted_with_line_numbers_preserved():
    diff = ("diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1,3 +1,4 @@\n"
            " def f(db, n):\n-    old = 1\n+    q = \"SELECT * FROM t WHERE a = '\" + n + \"'\"\n+    db.execute(q)\n")
    src, why = to_source(diff)
    assert why is None
    lines = src.split("\n")
    assert len(lines) == len(diff.split("\n"))
    assert lines[4] == "def f(db, n):" and lines[6].lstrip().startswith("q = ")
    assert lines[5] == "" and lines[0] == ""  # removed line and header are blanked


def test_non_python_diff_is_declined_not_misread():
    src, why = to_source("--- a/x.js\n+++ b/x.js\n@@ -1 +1 @@\n+eval(x)\n")
    assert src == "" and "Python only" in why


@needs_semgrep
async def test_rules_catch_each_seeded_bug_and_stay_quiet_on_controls(packs):
    g = SemgrepGrounder()
    items = [i for split in ("calibration", "heldout") for i in packs.corpus("code-review-dev", split)]
    for item, (findings, err) in zip(items, await g.check_many([i.input for i in items]), strict=True):
        assert err is None
        if item.label.has_issue:
            assert any(item.label.lines[0] - 2 <= f.line_start <= item.label.lines[1] + 2
                       and f.category == item.label.issue_category for f in findings), item.id
        else:
            assert findings == [], f"{item.id} is clean but the tool flagged {findings}"


@needs_semgrep
async def test_tool_works_on_a_diff_and_reports_pasted_line_numbers():
    diff = ("--- a/s.py\n+++ b/s.py\n@@ -1,2 +1,3 @@\n async def go(client):\n+    r = client.get('/x')\n"
            "     return 1\n")
    findings, err = await SemgrepGrounder().check(diff)
    assert err is None and [(f.category, f.line_start) for f in findings] == [("missing_await", 5)]


@needs_semgrep
async def test_injected_code_is_parsed_never_executed(tmp_path):
    marker = tmp_path / "pwned"
    code = f"import os\nos.system('touch {marker}')\n__import__('os').system('touch {marker}')\n"
    await SemgrepGrounder().check(code)
    assert not marker.exists()


async def test_missing_tool_is_reported_not_fatal():
    g = SemgrepGrounder(binary="/nonexistent/semgrep")
    assert not g.available()
    findings, err = await g.check(LAST)
    assert findings == [] and "not installed" in err


class StubGrounder:
    name = "semgrep"

    def __init__(self, findings, error=None):
        self.findings, self.error = findings, error

    def available(self):
        return True

    async def check(self, text):
        return self.findings, self.error


def tf(line, cat="off_by_one"):
    return ToolFinding(tool="semgrep", rule_id="r1", category=cat, line_start=line, line_end=line,
                       claim="tool says so")


async def run(debate, registry, text, **kw):
    m = ids(registry)
    events = []
    req = RunRequest(pack="code-review-dev", input=text, model_ids=[m[x] for x in ALL3], seed=3, **kw)
    res = await debate.run(req, "r1", lambda e, d: events.append((e, d)))
    return res, events


async def test_tool_finding_outranks_a_unanimous_panel(debate, registry):
    debate.grounder = StubGrounder([tf(2)])
    res, events = await run(debate, registry, LAST, ground=True)
    names = [e for e, _ in events]
    assert names.index("stopped") < names.index("grounded") < names.index("tally")
    f = res.final
    assert f.verdict.overall == "issue"
    c = f.confirmed_findings[0]
    assert c.tool_verified and c.supporters == [] and c.id == "t1" and c.vote_accept
    assert len(f.dissent) == 3  # every panelist missed it
    assert f.recommendation.tool_verified and f.next_step.cluster_id == "t1"
    assert res.ruling.rulings[0].accept and not res.ruling.overrides_vote


async def test_tool_marks_a_cluster_the_models_found_and_accepts_it_despite_a_low_vote(debate, registry):
    debate.grounder = StubGrounder([tf(2, "unbounded_query")])
    unb = ('def list_events(db, user_id):\n    rows = db.execute("SELECT * FROM events WHERE user_id = ?", (user_id,))\n'
           '    return [dict(r) for r in rows.fetchall()]\n')
    res, _ = await run(debate, registry, unb, ground=True, max_rounds=0,
                       judge_model_id=ids(registry)["judge-skeptic"])
    c = res.final.confirmed_findings[0]
    assert c.tool_verified and c.supporters and c.vote_accept
    # the skeptic judge rejects single-supporter clusters; the tool-verified one is untouchable
    assert not res.ruling.overrides_vote


async def test_judge_cannot_reject_a_tool_verified_cluster(debate, registry):
    debate.grounder = StubGrounder([tf(2)])
    m = ids(registry)
    p = registry.provider(m["sharp"])

    async def reject_all(messages, **kw):
        from quorum.providers.base import Completion
        if "You are the judge" in messages[0]["content"]:
            return Completion(json.dumps({"rulings": [
                {"cluster_id": "t1", "accept": False, "reason": "I disagree with the analyser entirely."}],
                "reason": "no"}), 5, 5)
        return await orig(messages, **kw)

    orig = p.chat
    p.chat = reject_all
    res, _ = await run(debate, registry, LAST, ground=True, judge_model_id=m["sharp"])
    assert res.final.confirmed_findings and not res.ruling.overrides_vote


async def test_ground_unavailable_or_failing_does_not_stop_the_run(debate, registry):
    debate.grounder = None
    res, events = await run(debate, registry, LAST, ground=True)
    g = next(d for e, d in events if e == "grounded")
    assert g.available is False and "semgrep" in g.error and res.final.verdict.overall == "no_issue"
    debate.grounder = StubGrounder([], error="semgrep timed out")
    res, events = await run(debate, registry, LAST, ground=True)
    assert next(d for e, d in events if e == "grounded").error == "semgrep timed out"
    assert res.final is not None


async def test_ground_is_off_by_default_and_ignored_for_claims(debate, registry):
    debate.grounder = StubGrounder([tf(2)])
    _, events = await run(debate, registry, LAST)
    assert "grounded" not in [e for e, _ in events] and events[0][1].ground is False
    m = ids(registry)
    ev = []
    req = RunRequest(pack="claim-check-dev", input="The Berlin Wall fell in 1989.", ground=True,
                     model_ids=[m[x] for x in ALL3], seed=3)
    await debate.run(req, "r2", lambda e, d: ev.append(e))
    assert "grounded" not in ev


async def test_eval_condition_six_uses_the_tool(debate, registry):
    m = ids(registry)
    models = [m[x] for x in ALL3]
    await calibrate_many(debate, models, ["code-review-dev"])
    debate.grounder = None
    res = await run_eval(debate, EvalRequest(pack="code-review-dev", model_ids=models,
                                             judge_model_id=m["sharp"]))
    assert res.conditions[5].available is False
    if not SemgrepGrounder().available():
        return
    debate.grounder = SemgrepGrounder()
    res = await run_eval(debate, EvalRequest(pack="code-review-dev", model_ids=models,
                                             judge_model_id=m["sharp"]))
    c6 = res.conditions[5]
    assert c6.available and c6.tp + c6.fp + c6.tn + c6.fn == 8
    assert c6.recall.value >= res.conditions[4].recall.value  # tools can only add accepted findings
    assert any("not evidence" in n for n in res.notes)
