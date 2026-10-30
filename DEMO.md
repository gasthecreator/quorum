# Quorum demo runbook

This runbook is for a macOS Apple Silicon machine with 8 GB of memory. It uses the three small
Ollama tags listed in the README, one model call at a time. They are candidates, not a validated
quality or latency lineup. The local checkout used to prepare this runbook has no Ollama installation
or downloaded models, so the live steps still need a real rehearsal.

## 1. Install and pull the candidate models

Install Ollama using its official macOS instructions, then open a terminal and pull the exact tags:

```bash
ollama pull qwen2.5:1.5b
ollama pull llama3.2:1b
ollama pull gemma3:1b
ollama --version
ollama list
```

Record the resolved tags/digests, Ollama version, macOS version, and available memory with any demo
results. These are different model families, but their small size can limit code-review accuracy.

## 2. Install Quorum

In one terminal, from the repository root:

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/quorum packs validate code-review
.venv/bin/quorum packs validate claim-check
```

In a second terminal, install the frontend dependencies and start the Vite app in live mode:

```bash
cd frontend
npm ci
VITE_API=live npm run dev
```

Open `http://127.0.0.1:5173`. The backend binds to `127.0.0.1:8787`; the frontend only accepts
that configured local API address. If Ollama is not already running, start it using the installed
Ollama app or its documented `ollama serve` command.

## 3. Run a code-review demo

In the UI, refresh the model registry and confirm the three tags appear as open-weight, local models.
Open **Model Manager** and calibrate the models on the `Code review` pack. Calibration runs real
inference over a small synthetic corpus; wait for each job to finish before comparing profiles.

Open **Review room** and use `demo/seeded-sql-injection.py` as the input. Select the code-review pack,
three different-family models, one peer-review round, and a judge. Start the review and inspect the
line-level findings, dissent, and ruling. Export the transcript if you need to share the recorded
run.

For a factual-claim example, switch to the `Claim check` pack and submit: “Mercury is the largest
planet in the solar system.” The NASA-linked held-out corpus labels and UI result are not a substitute
for an independent source check.

## 4. Inspect evaluation output

The **Evaluations** page runs the six backend conditions on the selected pack's held-out split and
shows the confusion counts, metrics, and intervals. Running an evaluation makes model inference calls
and may take a while. The current claim-check split contains only 20 claims; the code-review split has
14 synthetic examples. Those sizes are too small for stable comparative claims. Do not present a
leaderboard or security-performance conclusion from these corpora.

## Limits to state during the demo

- Model output is fallible; verify code findings and factual claims independently.
- Calibration measures performance on these particular synthetic items. It is not a general measure
  of model skill.
- Hosted providers send review input to that provider. The UI marks hosted models before a run.
- The three candidate local models and this complete live workflow have not been rehearsed in the
  current environment; do not describe them as validated until that rehearsal is done.
