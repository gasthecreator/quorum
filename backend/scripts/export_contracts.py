"""Write the machine-readable contract files under contracts/. Run after changing schemas.py or api.py.

    python scripts/export_contracts.py          # rewrite
    python scripts/export_contracts.py --check  # exit 1 if the committed files are stale
"""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from quorum import schemas
from quorum.api import create_app
from quorum.config import Settings


def build() -> dict[str, str]:
    s = Settings()
    s.home = Path(tempfile.mkdtemp())
    s.models_yaml = s.home / "none.yaml"
    openapi = create_app(s).openapi()
    events = {
        "contract_version": schemas.CONTRACT_VERSION,
        "envelope": schemas.EventEnvelope.model_json_schema(),
        "run_events": {k: v.model_json_schema() for k, v in schemas.RUN_EVENT_MODELS.items()},
        "job_events": {k: v.model_json_schema() for k, v in schemas.JOB_EVENT_MODELS.items()},
        "chat_events": {k: v.model_json_schema() for k, v in schemas.CHAT_EVENT_MODELS.items()},
        "terminal_events": {"run": ["final", "error"], "job": ["job_done", "job_error"], "chat": []},
    }
    pack = {
        "pack_json": schemas.Pack.model_json_schema(),
        "corpus_item": schemas.CorpusItem.model_json_schema(),
    }
    dump = lambda o: json.dumps(o, indent=2, sort_keys=True) + "\n"
    return {
        "VERSION": schemas.CONTRACT_VERSION + "\n",
        "openapi.json": dump(openapi),
        "events.schema.json": dump(events),
        "pack.schema.json": dump(pack),
    }


if __name__ == "__main__":
    out = ROOT / "contracts"
    files = build()
    if "--check" in sys.argv:
        stale = [n for n, body in files.items() if not (out / n).is_file() or (out / n).read_text() != body]
        if stale:
            print("stale contract files (run scripts/export_contracts.py and bump VERSION): " + ", ".join(stale))
            sys.exit(1)
        print("contracts up to date")
    else:
        for name, body in files.items():
            (out / name).write_text(body)
        print("wrote " + ", ".join(files))
