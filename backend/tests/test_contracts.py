import json
import subprocess
import sys
from pathlib import Path

from pydantic import TypeAdapter

from quorum import schemas

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "contracts" / "fixtures"


def test_committed_contract_files_are_current():
    r = subprocess.run([sys.executable, str(ROOT / "backend/scripts/export_contracts.py"), "--check"],
                       capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stdout + r.stderr


def test_fixtures_validate_against_schemas():
    index = json.loads((FIX / "index.json").read_text())
    reasons = set()
    for entry in index:
        lines = [json.loads(x) for x in (FIX / entry["file"]).read_text().splitlines()]
        assert [x["seq"] for x in lines] == list(range(1, len(lines) + 1))
        assert [x["t_ms"] for x in lines] == sorted(x["t_ms"] for x in lines)
        for x in lines:
            schemas.RUN_EVENT_MODELS[x["event"]].model_validate(x["data"])
        assert lines[-1]["event"] in {"final", "error"}
        names = [x["event"] for x in lines]
        if lines[-1]["event"] == "final":  # exactly one of stopped / finished_early precedes the tally
            assert sum(n in ("stopped", "finished_early") for n in names) == 1
        if entry["stop_reason"]:
            reasons.add(entry["stop_reason"])
    assert reasons == {"consensus", "stalemate", "max_rounds", "budget"}
    seen = {x["event"] for e in index for x in (json.loads(ln) for ln in (FIX / e["file"]).read_text().splitlines())}
    assert {"framed", "escalated", "finished_early", "grounded"} <= seen
    started = [json.loads(ln)["data"] for e in index if e["file"] == "roles.jsonl"
               for ln in (FIX / e["file"]).read_text().splitlines() if '"run_started"' in ln]
    assert started and started[0]["roles"]


def test_one_fixture_shows_a_judge_override():
    lines = [json.loads(x) for x in (FIX / "judge_override.jsonl").read_text().splitlines()]
    ruling = next(x for x in lines if x["event"] == "judge_ruling")["data"]
    assert ruling["overrides_vote"] is True and ruling["reason"]


def test_pack_files_validate(packs):
    adapter = TypeAdapter(schemas.CorpusItem)
    for p in packs.list():
        for split in ("calibration", "heldout"):
            for item in packs.corpus(p.id, split):
                adapter.validate_python(item.model_dump())
