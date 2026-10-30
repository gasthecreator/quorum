# Quorum backend

Python 3.11+, FastAPI, SQLite. Runs without the UI.

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q                      # 60 tests, no model server needed
.venv/bin/quorum serve                   # http://127.0.0.1:8787
```

## CLI

```bash
quorum models                            # list (Ollama models are auto-discovered)
quorum models add --provider ollama --tag qwen2.5:3b
quorum models add --provider digitalocean --tag llama3.3-70b-instruct --open-weight   # hosted
quorum models add --provider openai_compat --tag gemma-3-4b-it \
    --base-url http://127.0.0.1:1234/v1 --api-key-env LMSTUDIO_API_KEY --open-weight
quorum packs                             # packs found; `quorum packs validate <id>`
quorum calibrate --models qwen2.5:3b,llama3.2:3b,gemma3:4b --pack code-review
quorum review change.diff --pack code-review --models qwen2.5:3b,llama3.2:3b,gemma3:4b \
    --escalate auto --export report.html      # 2 models first; .md or .html transcript
quorum eval --pack code-review --models qwen2.5:3b,llama3.2:3b,gemma3:4b --judge qwen2.5:3b
quorum profiles
```

## Offline development without a model server

`QUORUM_ENABLE_FAKE=1` registers five **simulated** models (`fake:sharp`, `fake:sloppy`,
`fake:stubborn`, `fake:judge-skeptic`, `fake:broken`). They are rule-based stand-ins that read the
real prompts and return real JSON, so the whole pipeline runs end to end. They are not language
models and nothing measured with them may be published. Hand-made seed packs live in `dev_packs/`.

## Settings (environment)

| Variable | Default | Meaning |
| --- | --- | --- |
| `QUORUM_HOME` | `~/.quorum` | SQLite database and `eval/` exports |
| `OLLAMA_HOST` / `QUORUM_OLLAMA_URL` | `http://127.0.0.1:11434` | Ollama |
| `QUORUM_PACK_DIRS` | `<repo>/packs:<repo>/backend/dev_packs` | Pack search path |
| `QUORUM_MODELS_YAML` | `<repo>/config/models.yaml` | Pinned judge, extra endpoints |
| `QUORUM_PARALLELISM` | `1` | Concurrent model calls. Keep 1 on an 8 GB Mac |
| `QUORUM_TOKEN_BUDGET` / `QUORUM_TIME_BUDGET_S` | 60000 / 900 | Per-run budget; stops with reason `budget` |
| `QUORUM_ENABLE_FAKE` | off | Simulated models |

## How it works

| Module | Plan task | What it does |
| --- | --- | --- |
| `schemas.py` | B1 | Every wire type. Source of the files in `contracts/` |
| `registry.py`, `providers/` | B2 | Ollama discovery (license read from `/api/show`), OpenAI-compatible adapter |
| `runner.py` | B3 | Calls with timeout, retries, JSON extraction, one repair attempt |
| `engine.py` | B4, B6 | Blind round, anonymised debate rounds, evidence-gated changes, stop rules, judge |
| `cluster.py` | B5 | Groups findings by file and line overlap, scores `sum(weight x confidence)` |
| `calibrate.py`, `weights.py`, `stats.py` | B7 | Per-category Wilson lower bound; `weight = max(lower, 0.05)` |
| `export.py` | B14 | Markdown and self-contained HTML transcript of a finished run |
| `evaluate.py` | B8 | Six conditions on the held-out split, CSV and JSON |
| `api.py`, `streams.py`, `cli.py` | B9 | Endpoints, replayable SSE, CLI |

Design decisions worth knowing:

- **Evidence gate.** A model that changes its verdict or findings in a visible round must give a
  `changed_reason` of at least 12 characters; otherwise the change is discarded and the event says
  "Change rejected".
- **Judge override needs a reason.** An override without one is ignored and the weighted vote stands.
- **Weights.** `measured` (a calibrated cell for that category), `pooled` (the model's whole-pack
  result) or `default` (0.5, never calibrated). The source is on every supporter.
- **Framer (B11).** The judge model restates the input neutrally before round 0; debaters see the
  restatement next to the original, fenced as data and labelled possibly wrong. Calibration and
  evaluation skip it (`frame=false`), so the weights are measured on the inputs they are applied to.
- **Critique (B12).** Rounds 1 and 2 add three questions (strongest, biggest blind spot, missed by
  all) and show the peers in a different shuffled order per model.
- **Escalation (B13).** `auto` needs 3+ models: the best pooled-weight model plus the best model from
  a different family go first; if they agree at confidence >= 0.6 the run finishes with two calls and
  no judge call. Evaluation always uses `escalate=always`, so condition 5 is the full debate.
- **Final sections.** `agreements`, `clashes`, `blind_spots` and `next_step` are derived from the
  tally and critiques, with no extra model call; only the one-line recommendation can come from the
  judge, with a deterministic fallback.
- **Locality (B15).** `hosted` means the prompt leaves this machine (any non-loopback, non-private
  host, and always DigitalOcean). The DigitalOcean preset is the OpenAI-compatible adapter pointed at
  `https://inference.do-ai.run/v1` with the key read from `DIGITALOCEAN_MODEL_ACCESS_KEY` (our own
  name for the variable; override with `api_key_env`). It has not been called for real.
- **Failure handling.** A model that fails to answer is carried over (or `unsure`) and excluded from
  the denominator; if none answer the run ends with an `error` event. Calibration refuses to write
  a profile if a third of the items got no usable answer.
- **Injection.** Input and peer text sit between nonce-tagged fences, the system prompts call them
  data, the input is scanned for steering phrases, and the judge is warned when any are found.
  Detection is a heuristic; the real defence is the anonymous weighted vote.
- **Evaluation honesty.** Weights come from the calibration split only, the evaluation refuses a
  held-out split that shares an id or identical input with calibration, condition 6 (tools) reports
  as unavailable until B10 exists, and every proportion has a 95% interval.

## Not done yet

- **Real-model validation.** Ollama is not installed on this machine, so the adapters are tested
  against an HTTP stand-in (`tests/fake_ollama.py`) and with mocked responses, never against a real
  model. First real run will need prompt tuning for small models.
- **B16 roles and B17 MCP wrapper** (stretch) are not started.
- **B10 tool grounding** (Semgrep, tests). Stretch; not started.
- The model lineup, parallelism and judge model wait on hardware (8 GB M2).
