# Quorum: Build Plan (Gideon + Elikem)

Oct 30, 2026 · @Gideon

## Goal and scope

Quorum is a local app where open-weight models debate a question or code diff for a capped number of rounds, a judge issues the final ruling, and each model's vote is weighted by how well it measured on that kind of problem.

**Headline demo:** paste a diff with seeded bugs. Three models review it blind, read each other's findings, revise, and a judge rules. The screen shows who changed their mind, who dissented, and whether the judge agreed with or overrode the weighted vote.

**Must ship**

- Debate engine: one blind round, then up to 2 visible rounds; stop on consensus, stalemate or the cap
- Per-category skill profiles from a labeled corpus, used as vote weights
- A judge with a visible override flag
- Chat-style UI plus a skill profiles page
- Model Manager: add an Ollama tag or any OpenAI-compatible URL; adding a model starts calibration
- Code-review pack (headline) and claim-check pack (shows it works beyond development)
- Held-out evaluation with honest numbers

**Stretch:** tool grounding (Semgrep, tests) that outranks votes; CI or PR comment output; more packs.

**Out of scope:** accounts, hosted deployment, fine-tuning, and any closed model in the demo. The demo uses open-weight models only.

## Architecture and contract

The two workstreams meet only at the HTTP and SSE contract below. It lives in `contracts/` and is frozen after checkpoint C0. Backend: `http://127.0.0.1:8787`. Frontend: `http://127.0.0.1:5173`.

**Endpoints**

| Method and path | Purpose |
| --- | --- |
| GET /health | Backend and Ollama status |
| GET /packs | Available packs and their categories |
| GET /models | Registered models: id, provider, tag, family, license, open\_weight, status |
| POST /models | Add a model (Ollama tag or OpenAI-compatible URL); starts a calibration job |
| GET /profiles | Skill profile per model per category |
| POST /runs | Start a debate: pack, input, model\_ids, max\_rounds (default 2) |
| GET /runs/{id}/events | SSE stream of the debate |
| GET /runs/{id} | Final stored result |
| POST /calibrate | Calibrate chosen models on a pack |
| POST /eval | Run held-out evaluation; GET /eval/latest returns the table |
| GET /jobs/{id}/events | SSE progress for calibration and eval jobs |

**Run events (SSE)**

| Event | Payload |
| --- | --- |
| run\_started | run\_id, pack, model\_ids, aliases |
| round\_started | round (0 = blind) |
| model\_message | round, model\_id, alias, position, findings, changed, changed\_reason, text |
| round\_finished | round, agreement (0 to 1) |
| stopped | reason: consensus, stalemate, max\_rounds or budget |
| tally | clusters with supporters, weights used, weighted score |
| judge\_ruling | ruling per cluster, overrides\_vote (true or false), reason |
| final | verdict, confirmed findings, disputed findings, dissent |
| error | code, message |

**Core objects**

| Object | Fields |
| --- | --- |
| Position | verdict (issue, no\_issue, true, false, unsure), confidence 0 to 1 |
| Finding | id, category, file, line\_start, line\_end, severity, claim, evidence |
| Profile cell | n, correct, rate, lower (Wilson 95% lower bound), weight |
| Pack | id, name, categories, modes, corpus paths (calibration, heldout) |
| Corpus item | id, category, input, label (has\_issue, issue\_category, lines; or true or false with a source url) |

Debaters see only anonymous aliases (A, B, C) and never see the weights. The UI shows real model names and weights. Text from the input and from peers is data, never instructions.

## Ownership and collision rules

Each person owns separate folders, and the contract is the only shared surface.

| Path | Owner | Rule |
| --- | --- | --- |
| backend/ | Gideon | Elikem never edits |
| frontend/ | Elikem | Gideon never edits |
| contracts/ | Gideon writes, Elikem reviews | Frozen after C0; changes by pull request labeled contract, approved by the other person, version bumped |
| contracts/fixtures/ | Gideon | Recorded event streams, at least one per stop reason |
| packs/ | Elikem | Plain files the backend reads at runtime by path; format fixed by the contract |
| docs/ and README.md | Elikem | Gideon sends facts through an issue comment |
| config/models.yaml | Gideon | Elikem uses the Model Manager UI instead |

**Working rules**

- Branches: `backend/<topic>` and `frontend/<topic>`. Every merge to main is a pull request, rebased on main first.
- The frontend builds against a mock server that replays the fixtures, so it never waits on the backend. Switch with `VITE_API=mock` or `live`.
- The backend runs without the UI through the CLI and curl.
- Ports are fixed (8787 and 5173). The backend allows CORS for 5173 only.
- Never commit model weights, `.env` files or secrets.
- If you are blocked by the other side, open an issue titled `contract: ...` instead of editing their folder.

## Gideon: backend and engine

Everything runs without the UI: `quorum review file.diff`, `quorum calibrate`, `quorum eval`, `quorum serve`.

| Task | Output | Done when |
| --- | --- | --- |
| B1 Contracts and fixtures | `contracts/` schemas and 3 event streams (recorded or hand-made) | Elikem can build the whole UI against the mock |
| B2 Model registry | Ollama auto-discovery, OpenAI-compatible adapter, metadata (family, license, open\_weight) | GET /models lists installed models; POST /models adds one |
| B3 Runner | Async parallel calls, structured JSON output, timeouts, retries | Three models answer one prompt in parallel with valid JSON |
| B4 Debate engine | Round 0 blind; rounds 1 and 2 with anonymized peer messages; hold or revise with evidence; flip log; stop on consensus, stalemate, cap or budget; summaries of earlier rounds | A full run streams the SSE events in the contract |
| B5 Clustering and tally | Group findings across models by file, line overlap and category; cluster score = sum of weight times confidence | The same bug flagged by three models becomes one cluster |
| B6 Judge | Anonymized prompt, sees the tally, overrides only with a stated reason, guard against instructions hidden in the code | Override flag and reason appear in judge\_ruling |
| B7 Calibration | Runs a pack's calibration corpus; writes profiles with n and the Wilson lower bound; weight = max(lower bound, 0.05) | GET /profiles returns real numbers for every model and category |
| B8 Evaluation | Held-out split under the 6 conditions in Evaluation; CSV and JSON | GET /eval/latest returns the table |
| B9 API and CLI | All endpoints and SSE streams in the contract | Frontend live mode works |
| B10 Stretch: tool grounding | Semgrep or test runs; a tool-confirmed finding outranks any vote | Findings tagged `tool_verified` |

**Order:** B1, then B2 and B3, then B4 to B6, then B7, B9 and B8, then B10.

## Elikem: frontend, packs and docs

Build against the mock server first, then switch to live.

| Task | Output | Done when |
| --- | --- | --- |
| F0 Scaffold | Vite, React, TypeScript, typed API client from `contracts/`, mock server replaying fixtures | App renders a recorded run end to end |
| F1 Debate view | Chat transcript, a colored lane per model, round dividers, flip badges, agreement meter | A fixture run plays smoothly in real time |
| F2 Input | Paste or upload a diff or claim; choose pack, models and round cap | POST /runs starts a run in live mode |
| F3 Verdict | Cluster board with weight bars, judge card, override badge, dissent list | Matches the judge\_ruling and final events |
| F4 Profiles | Heatmap of model by category showing weight, n and interval | Reads GET /profiles |
| F5 Model Manager | List, add an Ollama tag or URL, calibration progress, license and open-weight badge | Adding a model shows its profile when the job finishes |
| F6 Eval page | Table and chart of the conditions with intervals | Reads GET /eval/latest |
| P1 Code-review pack | `mutate.py`, 12 to 15 small clean functions, bug types (SQL injection, off-by-one, missing await, unbounded query and similar), clean controls, labeled calibration and held-out splits | Labels are generated automatically and spot-checked; the splits do not overlap |
| P2 Claim-check pack | About 40 claims with verified true or false labels and a source link each | Every label has a source someone can open |
| P3 README and demo | Install steps, model attribution and license table, honest limitations, `DEMO.md` script, demo recording | A stranger can reproduce the demo |

**Order:** F0, F1 and P1 first (Gideon's calibration needs the corpus), then F2 and F3, then F5, F4, F6, P2, P3.

## Integration checkpoints

Each checkpoint needs both sides done. C2 is the demo-ready line. If organizers do not accept submissions after the demo slot, everything after C2 matters only for the README.

| Gate | Both sides deliver | Pass test |
| --- | --- | --- |
| C0 Contract freeze | Gideon: schemas and fixtures. Elikem: review and issues | Elikem's mock renders a full fixture run |
| C1 First live run | One model, hardcoded weights, blind round only | UI in live mode shows a real model's findings |
| C2 Live debate | Three models, two rounds, tally, judge | A seeded-bug diff produces a clustered, ruled result in the UI |
| C3 Real weights | Elikem's corpus feeds Gideon's calibration | Profiles page shows measured weights with n |
| C4 Model Manager | Add a model from the UI | The new model appears with a calibrated profile |
| C5 Evaluation and docs | Eval table, README, demo script | Held-out numbers published, even if the panel does not win |

**Dependency to watch:** B7 needs P1. If P1 slips, Gideon uses a hand-made corpus of 6 items to keep moving and swaps in the real one at C3.

## Evaluation and honest claims

The panel earns its place only if it measures better than simpler options, so we test on held-out items the models never saw during calibration.

| Condition | What it is |
| --- | --- |
| 1 Best single model | Highest calibrated model alone |
| 2 Majority vote | Blind round, equal weights |
| 3 Weighted vote | Blind round, calibrated weights |
| 4 Weighted plus judge | Adds the judge ruling |
| 5 Debate, weighted, judge | Adds rounds 1 and 2 |
| 6 Condition 5 plus tools | Adds tool grounding, if built |

Metrics per condition: precision, recall, F1 and false-positive rate on clean items, each with a 95% interval. Also report how often a debate round moved a model toward the right answer versus the wrong one.

**What we claim.** If conditions 4 to 6 do not beat condition 1, the README says so and presents Quorum as a tool for surfacing disagreement and profiling local models. Published work found that multi-agent debate often fails to beat simple baselines, so a null result is plausible. The corpus is synthetic (mutation-seeded) and small, and the README states the sample size next to every weight.

Sources: [Zhang et al., Stop Overvaluing Multi-Agent Debate](https://arxiv.org/abs/2502.08788v3), [Du et al., Multiagent Debate](https://arxiv.org/pdf/2305.14325)

## Risks, open decisions and submission

**Open decisions**

- Hardware: an Apple Silicon MacBook Pro; memory and installed models are not confirmed yet. The model lineup, parallelism and judge model wait on that.
- Submission rule: whether projects submitted after the 4:30 demo slot are judged. Ask an organizer.
- Final product name.

**Risks and mitigations**

| Risk | Mitigation |
| --- | --- |
| Three models plus a judge are slow on one laptop | Cap output length, two rounds, run in parallel where memory allows, summarize earlier rounds |
| Models from one family err together | Choose three different families |
| Debate makes models agree on a wrong answer | Blind first round, hidden weights, evidence required to change a position, ablation reported |
| Small calibration samples give noisy weights | Wilson lower bound, weight floor, show n |
| Code under review tries to steer the models | Treat all input and peer text as data; include one injection test case |
| Model licenses differ | Record each license in the README |
| Contract drift | Contract changes only by labeled pull request |

**Submission checklist**

- [ ] Public GitHub repository with an MIT license for our code
- [ ] README with install steps, exact model names, versions and licenses
- [ ] Demo path rehearsed with real inference, nothing mocked
- [ ] Held-out evaluation results included
- [ ] Prior art cited: llm-council-skill and the debate papers
- [ ] Devpost submission sent before the deadline the organizers confirm
