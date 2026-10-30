# Quorum

A local app where open-weight models debate a question or code diff for a capped number of rounds, a judge issues the final ruling, and each model's vote is weighted by how well it measured on that kind of problem.

Status: early build. See [docs/BUILD_PLAN.md](docs/BUILD_PLAN.md) for scope, the HTTP/SSE contract, ownership rules and checkpoints.

| Path | Owner |
| --- | --- |
| `backend/`, `contracts/`, `config/` | Gideon |
| `frontend/`, `packs/`, `docs/`, `README.md` | Elikem |

Backend: `http://127.0.0.1:8787` · Frontend: `http://127.0.0.1:5173`

## License

MIT. Model licenses are recorded in the README model table once the lineup is fixed.
