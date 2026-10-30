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
Live mode loads contract v1.1 packs and model metadata, accepts pasted or uploaded text, and submits
the selected pack, open-weight panel, judge, round cap, framing, and escalation policy to `POST /runs`.
Hosted model selections are labeled and trigger a notice that the review input goes to that provider.

The Model Manager registers an installed Ollama tag, an OpenAI-compatible endpoint, or a
DigitalOcean inference deployment. It can start calibration against selected packs and streams job
progress. For API keys, enter an environment-variable name in the UI and set that variable in the
backend process environment; the key itself is never sent by the browser. The Skill Profiles page
shows observed calibration rate, sample count, and the Wilson 95% lower bound used as the vote weight.

The generated code-review corpus is in `packs/code-review/`: 12 calibration and 14 held-out synthetic
examples. It is an integration seed, far too small to support stable model rankings or security claims.

## License

MIT. Model licenses are recorded in the README model table once the lineup is fixed.
