# Quorum contract

Version: see [`VERSION`](VERSION) (currently 1.3.0). 1.3 adds roles and the outsider. 1.1 added the framer, critique round, richer `final`,
escalation, export and locality; 1.2 adds tool grounding (`ground`, `grounded`, tool-verified clusters).
Nothing was removed or renamed since 1.0. This folder is the only surface the backend and
frontend share. The Python models in `backend/quorum/schemas.py` are the source of truth;
`backend/scripts/export_contracts.py` generates the JSON files here, and a test fails if they drift.

| File | What it is |
| --- | --- |
| `openapi.json` | Every HTTP route, request body and response body |
| `events.schema.json` | JSON Schema of every SSE `data` payload (run events and job events) |
| `pack.schema.json` | Schema of `pack.json` and of one corpus line |
| `fixtures/*.jsonl` | Recorded run streams for the mock server (see below) |

Backend `http://127.0.0.1:8787`, frontend `http://127.0.0.1:5173` (the only CORS origin).

## Changing the contract

Frozen after checkpoint C0. Open a pull request labeled `contract`, regenerate the files
(`python backend/scripts/export_contracts.py`), bump `VERSION` (and `CONTRACT_VERSION` in
`schemas.py`), and get approval from the other person. CI runs `--check`.

## Errors

Every non-2xx response is `{"code": str, "message": str}`. Codes the UI should handle:
`validation_error` (422), `unknown_run`, `unknown_job`, `unknown_model`, `pack_error` (404),
`model_unavailable` (409), `model_not_installed` (409), `run_not_finished` (409), `ollama_unreachable` (503),
`base_url_required`, `api_key_missing`, `fake_disabled` (400), `no_eval` (404).

## Endpoints

| Method and path | Body | Response |
| --- | --- | --- |
| `GET /health` | | `Health` (`ollama.reachable`, `fake_models_enabled`, ...) |
| `GET /packs` | | `Pack[]` with `corpus_counts` |
| `GET /models` | | `ModelInfo[]` (refreshes from Ollama first) |
| `POST /models` | `ModelCreate` | 201 `ModelCreated {model, job_id}`; `job_id` is a calibration job |
| `GET /profiles?pack=` | | `{profiles: model_id -> pack_id -> category -> ProfileCell, default_weight}` |
| `POST /runs` | `RunRequest` | 202 `{run_id}` |
| `GET /runs/{id}/events` | | SSE, see below |
| `GET /runs/{id}` | | `RunSummary` (status, stored events, `final`) |
| `GET /runs/{id}/export?format=md\|html` | | A download: Markdown transcript or self-contained HTML report, aliases revealed, all model text escaped. 409 `run_not_finished` while running |
| `POST /calibrate` | `CalibrateRequest` | 202 `{job_id}` |
| `POST /eval` | `EvalRequest` | 202 `{job_id}` |
| `GET /eval/latest?pack=` | | `EvalResult`; 404 `no_eval` before the first run |
| `GET /jobs/{id}` | | `Job` (poll alternative to SSE) |
| `GET /jobs/{id}/events` | | SSE of `job_progress`, then `job_done` or `job_error` |

## SSE

Each frame is `id: <seq>`, `event: <name>`, `data: <json>`. `seq` starts at 1 and has no gaps.
Reconnect with the `Last-Event-ID` header (or `?after=<seq>`) to resume; a finished stream replays in
full. The server sends `: ping` comments every 15 s.

**Close the `EventSource` yourself after a terminal event** (`final` or `error` for runs,
`job_done` or `job_error` for jobs), otherwise the browser reconnects and replays the stream.

Run events, in the order they occur:

```
run_started
[framed]                                                     # frame=true: neutral restatement, before round 0
round_started(0) model_message x N round_finished(0)        # blind round, messages arrive as each model finishes
[round_started(1) model_message x N round_finished(1)]      # up to max_rounds visible rounds
[round_started(2) ...]
stopped(reason: consensus | stalemate | max_rounds | budget)   # or finished_early, never both
[grounded]                                                   # ground=true, review packs: static-analysis results
tally  ->  judge_ruling  ->  final
```

With `escalate: "auto"` (three or more models) round 0 is split in two:

```
run_started
round_started(0) model_message x 2                           # best calibrated model + best other-family model
  agree with confidence >= 0.6:
      round_finished(0) -> finished_early -> tally -> judge_ruling(skipped: true) -> final
  otherwise:
      escalated(reason: disagreement | low_confidence)
      model_message x (N-2)  round_finished(0)               # the rest join round 0, then the normal loop
      ... stopped -> tally -> judge_ruling -> final
```

A run that ends in `error` has no `final`. `model_message` with `error` set means that model failed to
answer that round (its position is carried over, or `unsure` at confidence 0 in round 0).
`escalate: "always"` (the default) skips all of that and runs the full debate. `frame` defaults to
true for `always` and false for `auto`, so an easy input costs exactly two model calls.

**Tool grounding** (`ground: true`, review packs only, off by default): after the debate stops, Semgrep
is run over the pasted input (a diff is analysed with its line numbers preserved) and a `grounded`
event reports what it found. A finding that overlaps a cluster marks it `tool_verified: true` and
accepts it whatever the vote said; a finding no panelist raised becomes its own cluster
(`id` "t1", ...) with `supporters: []`, `weighted_score: 0` and `vote_accept: true`. The judge cannot
reject a tool-verified cluster. `grounded.available` is false (with `error`) when Semgrep is missing,
and the run carries on; `GET /health` reports `grounding.semgrep`. The rules are heuristics written for
precision, cover Python only, and live in `backend/quorum/rules/python.yml`.

**Roles** (`roles: "profile"`, review packs only, off by default): each model gets a one- or
two-sentence angle (`attacker`, `maintainer` or `operator`) matched to the categories it measured best
at; `run_started.roles` maps model_id to `{role, source, detail}` and every `model_message` carries
its `role`. `source` is `measured` (from the profile), `default` (not calibrated yet, so assigned by
order; show it as provisional) or `chosen` (the outsider). `outsider: true` (3+ models) takes the
weakest specialist out: it reviews the bare input in round 0 with no framing and no peers, never
appears in later rounds, and its vote still counts. Whether roles help is an open question: the
evaluation can report a roles-on condition 7 (`EvalRequest.roles`) beside condition 5.
A pack may pin a category to a role with `category_roles` in `pack.json`; otherwise the backend
guesses from the category name.

**The framer** (`framed`): one extra call, made by the judge model, that restates the input in
neutral words so a leading prompt cannot steer every model the same way. Debaters get it as a fenced
block next to the original input, never instead of it. `framed.error` is set if the call failed;
the run continues without a framing. If the restatement itself contains steering text,
`framed.input_flags` lists it.

**The critique** (`model_message.critique`, rounds 1 and 2 only): each model answers three short
questions about the other panelists' messages, which arrive in a different shuffled order for each
model: `strongest`, `biggest_blind_spot`, `missed_by_all`. A model may answer "none".

Things worth showing in the UI, and where they come from:

| UI element | Field |
| --- | --- |
| Real model names; the debaters only saw aliases | `run_started.aliases` (model_id to A, B, C) |
| Flip badge | `model_message.changed`, with `changed_reason` as the tooltip |
| A change that was refused for lack of evidence | `changed == false` and `changed_reason` starts with "Change rejected" |
| Agreement meter | `round_finished.agreement`, 0 to 1 |
| Weight bars | `tally.clusters[].supporters[].weight` and `weight_source` (`measured`, `pooled`, `default`) |
| Sample size next to a weight | `GET /profiles` cell `n` (the plan requires showing it) |
| Override badge | `judge_ruling.overrides_vote`, per cluster `rulings[].overrides_vote`, reason in `rulings[].reason` |
| Input is trying to steer the models | `run_started.input_flags`, `judge_ruling.injection_flags` |
| Dissent list | `final.dissent` |
| "Everyone agreed" / "they clashed, here are both sides" | `final.agreements`, `final.clashes[].for_side / against_side` |
| Blind spots panel | `final.blind_spots[]` (critic alias, kind, text), plus each message's `critique` |
| Recommendation line | `final.recommendation.text` and `.confidence` (weight x confidence of the panelists who agree / total weight) |
| Top fix with a patch | `final.next_step.text`, `.patch` (null unless a panelist supplied replacement code) |
| "Answered by 2 models" vs "escalated, because ..." | `finished_early` (`calls_saved` is a lower bound) or `escalated.reason / detail`; also `run_started.escalate` |
| Judge skipped | `judge_ruling.skipped` |
| Role badge on a lane | `model_message.role`, `run_started.roles[model_id].source` |
| "Confirmed by a tool" badge; no supporters | `cluster.tool_verified`, `cluster.tool_rule`, `supporters == []`; `recommendation.tool_verified` (its confidence still reflects the panel, so it can be 0) |
| Local vs hosted badge | `GET /models[].locality` (a hosted model sends the diff off this machine) |

`tally.clusters[].kind` is `finding` for the code-review pack (lines, category, severity) and
`verdict` for the claim-check pack (`verdict` is `true` or `false`). `vote_accept` is the weighted
vote before the judge; a cluster is confirmed when its ruling has `accept: true`.

`fix` on a cluster and `final.next_step.patch` come from the panelists' optional per-finding `fix`.

`weight_source` matters: `default` means the model was never calibrated on this pack and a flat
0.5 was used, so the bar should look provisional.

## Line numbers

For the code-review pack, models see the input with a gutter and cite those numbers, so
`line_start`/`line_end` are 1-based lines of the pasted text itself (not of the file in a repo).
Corpus labels use the same convention. Two findings are the same cluster if their ranges overlap
within 2 lines.

## Packs

A pack is a folder the backend reads at runtime. Search order: `packs/` in the repo, then
`backend/dev_packs/` (hand-made seed packs for development; a pack id in `packs/` wins).

```
packs/<pack-id>/pack.json
packs/<pack-id>/calibration.jsonl     # path comes from pack.json
packs/<pack-id>/heldout.jsonl
```

`pack.json` (schema in `pack.schema.json`):

```json
{
  "id": "code-review",
  "name": "Code review",
  "description": "...",
  "categories": ["sql_injection", "off_by_one", "missing_await", "unbounded_query"],
  "category_descriptions": {"sql_injection": "User input concatenated into SQL."},
  "category_roles": {"sql_injection": "attacker"},
  "modes": ["review"],
  "corpus": {"calibration": "calibration.jsonl", "heldout": "heldout.jsonl"}
}
```

`id` must equal the folder name. `modes` is `["review"]` or `["claim"]`; the first entry is used.
`category_descriptions` is optional but is shown to the models, so write it. `category_roles` is
optional (see Roles above).

One JSON object per line in each corpus file:

```json
{"id": "cal-017", "category": "sql_injection", "input": "def f(db, n):\n ...\n",
 "label": {"has_issue": true, "issue_category": "sql_injection", "lines": [2, 2]}}
{"id": "cal-018", "category": "sql_injection", "input": "...",
 "label": {"has_issue": false, "issue_category": null, "lines": null}}
{"id": "c-001", "category": "science", "input": "Water boils at ...",
 "label": {"truth": true, "source_url": "https://en.wikipedia.org/wiki/Boiling_point"}}
```

Rules, enforced by `quorum packs validate <id>`:

- `category` of every item is listed in `pack.json`. A clean control carries the category of the
  bug it is a control for, so each category's weight is measured on bugs and clean code.
- Review labels: `has_issue: true` needs `lines` (1-based, inclusive, in `input`).
  Claim labels: `truth` plus an openable `source_url`.
- Ids are unique, and **no heldout item shares an id or identical input with a calibration item**.
  The evaluation refuses to run on a contaminated split.
- Label type matches the pack mode.

## Fixtures

`fixtures/*.jsonl` hold one event per line: `{"seq", "t_ms", "event", "data"}`. `t_ms` is a
synthetic clock from the start of the run, so a mock server can replay with `setTimeout` at the
original pacing. `fixtures/index.json` lists each file, what it shows, its stop reason and the
request that produced it.

| Fixture | Shows |
| --- | --- |
| `consensus` | Stops after the blind round (includes the `framed` event) |
| `stalemate` | A model is persuaded, another holds; nobody moves in the last round |
| `max_rounds` | Positions still moving at the cap |
| `budget` | Stops after round 0 on the token budget |
| `judge_override` | The judge overrides the weighted vote with a reason |
| `injection` | Input steers one model; flagged and outvoted |
| `claim_check` | Verdict clusters for the claim pack |
| `escalated` | `escalate: auto`, the first two disagree, the third joins round 0 |
| `finished_early` | `escalate: auto`, two agreeing models, no framer, judge skipped, 2 calls |
| `grounded` | `ground: true`: Semgrep finds an off-by-one no panelist saw; a tool-only cluster is accepted |
| `roles` | `roles=profile` + `outsider`: role badges, and the outsider speaks in round 0 only |
| `error` | A stream that ends in `error` |

These streams come from the **simulated models** (`QUORUM_ENABLE_FAKE=1`), which are rule-based
stand-ins, not language models. They are real engine output with realistic shapes; the wording and
confidences are not representative of Ollama models. Regenerate with
`python backend/scripts/record_fixtures.py`.
