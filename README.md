# Quorum

A local app where open-weight models debate a question or code diff for a capped number of rounds, a judge issues the final ruling, and each model's vote is weighted by how well it measured on that kind of problem.

Status: early build. See [docs/BUILD_PLAN.md](docs/BUILD_PLAN.md) for scope, the HTTP/SSE contract, ownership rules and checkpoints.

| Path | Owner |
| --- | --- |
| `backend/`, `contracts/`, `config/` | Gideon |
| `frontend/`, `packs/`, `docs/`, `README.md` | Elikem |

Backend: `http://127.0.0.1:8787` · Frontend: `http://127.0.0.1:5173`

## Frontend demo

The first frontend slice replays the checked-in SSE fixtures without starting Ollama or the backend:

```bash
cd frontend
npm install
npm run dev
```

Open `http://127.0.0.1:5173`. Fixture replay is the default (`VITE_API=mock`). To use the live API,
start `quorum serve` from `backend/` in a second terminal and run `VITE_API=live npm run dev`.
Live mode loads pack and model metadata, accepts pasted or uploaded text, and submits
the selected pack, open-weight panel, judge, round cap, framing, and escalation policy to `POST /runs`.
Hosted model selections are labeled and trigger a notice that the review input goes to that provider.
For code-review packs, live mode can also assign roles from calibration profiles, include a cold
outsider, and run Semgrep grounding when the backend reports Semgrep available. Evaluations can add
the role ablation as condition 7.

The Model Manager registers an installed Ollama tag, an OpenAI-compatible endpoint, or a
DigitalOcean inference deployment. It can start calibration against selected packs and streams job
progress. For API keys, enter an environment-variable name in the UI and set that variable in the
backend process environment; the key itself is never sent by the browser. The Skill Profiles page
shows observed calibration rate, sample count, and the Wilson 95% lower bound used as the vote weight.
The Evaluations page compares the backend’s six baseline conditions and can include a seventh
role-ablation condition; it shows metric intervals, and evaluation runs make inference calls to the
selected providers.

The generated code-review corpus is in `packs/code-review/`: 12 calibration and 14 held-out synthetic
examples. It is an integration seed, far too small to support stable model rankings or security claims.
The source-linked claim-check corpus is in `packs/claim-check/`: 20 calibration and 20 held-out claims
across astronomy, SI units, hydrology, and oceanography. The source URL for each claim is in its label.

## Candidate local demo models

The plan records an Apple M2 with 8 GB memory. The following small, distinct-family Ollama tags are
reasonable candidates for trying one at a time; none are installed or validated in this checkout.
Listed sizes are Ollama download artifacts, not peak memory use. Tags and library listings can change,
so record the resolved digest and exact Ollama version after pulling them before publishing results.

| Ollama tag | Family | Listed artifact size | License |
| --- | --- | ---: | --- |
| [qwen2.5:1.5b](https://ollama.com/library/qwen2.5:1.5b) | Qwen 2.5 | 986 MB | [Apache-2.0](https://www.apache.org/licenses/LICENSE-2.0) |
| [llama3.2:1b](https://ollama.com/library/llama3.2:1b) | Llama 3.2 | 1.3 GB | [Llama 3.2 Community License](https://dev.meta.ai/llama/llama3_2/license) |
| [gemma3:1b](https://ollama.com/library/gemma3:1b) | Gemma 3 | 806 MB | [Gemma Terms of Use](https://ai.google.dev/gemma/terms) |

See the runnable sequence and limitations in [DEMO.md](DEMO.md). Parallelism is set to one for the
8 GB machine; these candidates have not been benchmarked for review quality or latency.

## License

Quorum's code is MIT licensed. The model licenses above apply to their respective weights and are not
MIT licenses for those weights. Read each model's license terms before use or distribution.
