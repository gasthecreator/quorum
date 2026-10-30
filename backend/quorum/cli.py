"""Command line: everything works without the UI.

    quorum serve
    quorum models [add --provider ollama --tag qwen2.5:3b]
    quorum packs [validate PACK]
    quorum review file.diff --pack code-review --models a,b,c
    quorum calibrate --models a,b,c --pack code-review
    quorum eval --pack code-review --models a,b,c
    quorum profiles
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

from .api import AppState
from .calibrate import calibrate_many
from .config import Settings
from .evaluate import run_eval
from .export import to_html, to_markdown
from .registry import RegistryError
from .schemas import EvalRequest, ModelCreate, RunRequest
from .weights import WeightBook


def _state() -> AppState:
    s = AppState(Settings.from_env())
    s.registry.load_config()
    return s


def _resolve(state: AppState, names: list[str]) -> list[str]:
    models = state.store.list_models()
    out = []
    for n in names:
        hit = [m for m in models if m["id"] == n] or [m for m in models if m["tag"] == n]
        if len(hit) != 1:
            raise SystemExit(f"model '{n}' is {'ambiguous' if hit else 'not registered'}; "
                             "see `quorum models`")
        out.append(hit[0]["id"])
    return out


def _default_models(state: AppState, n: int = 3) -> list[str]:
    usable = [m for m in state.store.list_models()
              if m["status"] in ("ready", "registered") and m["tag"] != "broken"
              and not m["tag"].startswith("judge")]
    ready = sorted(usable, key=lambda m: m["status"] != "ready")
    seen, picks = set(), []
    for m in ready:  # prefer different families
        if m["family"] not in seen:
            seen.add(m["family"])
            picks.append(m["id"])
    return picks[:n]


def _fmt_event(event: str, d: dict) -> str | None:
    if event == "run_started":
        al = ", ".join(f"{a}={m}" for m, a in d["aliases"].items())
        flags = f"  !! input looks like it is steering the models: {d['input_flags']}" if d["input_flags"] else ""
        return f"Run {d['run_id']}  [{d['pack']}]  {al}  judge={d['judge_model_id']}{flags}"
    if event == "framed":
        return ("Framed: " + (d["restatement"] if d["restatement"] else f"(failed: {d['error']})")
                + (f"  !! steering text in the framing: {d['input_flags']}" if d["input_flags"] else ""))
    if event == "grounded":
        if not d["available"] or d["error"]:
            return f"Static analysis ({d['tool']}): not run - {d['error']}"
        got = "; ".join(f"L{f['line_start']} {f['category']} ({f['rule_id']})" for f in d["findings"])
        return f"\nStatic analysis ({d['tool']}, {d['duration_ms']} ms): {got or 'nothing found'}"
    if event == "escalated":
        return (f"  >> escalated ({d['reason']}): {d['detail']} adding {len(d['added_model_ids'])} model(s)")
    if event == "finished_early":
        return f"\nfinished early: {d['reason']} ({d['calls_saved']} model calls saved)"
    if event == "round_started":
        return f"\n-- round {d['round']}{' (blind)' if d['round'] == 0 else ''} --"
    if event == "model_message":
        p = d["position"]
        where = ", ".join(f"L{f['line_start']}-{f['line_end']} {f['category']}" for f in d["findings"])
        flip = f"  << CHANGED: {d['changed_reason']}" if d["changed"] else (
            f"  (kept; {d['changed_reason']})" if d["changed_reason"] else "")
        err = f"  ERROR: {d['error']}" if d["error"] else ""
        crit = d.get("critique")
        extra = (f"\n       critique: strongest={crit['strongest'][:60]} | blind spot="
                 f"{crit['biggest_blind_spot'][:60]} | missed={crit['missed_by_all'][:60]}") if crit else ""
        return f"  {d['alias']}  {p['verdict']:<8} {p['confidence']:.2f}  {where}{flip}{err}{extra}"
    if event == "round_finished":
        return f"  agreement {d['agreement']:.2f}"
    if event == "stopped":
        return f"\nstopped: {d['reason']} (after round {d['round']})"
    if event == "tally":
        lines = ["\nTally:"]
        for c in d["clusters"]:
            sup = ",".join(f"{s['alias']}({s['weight']:.2f})" for s in c["supporters"])
            at = f"L{c['line_start']}-{c['line_end']} " if c["kind"] == "finding" else ""
            tool = f" [TOOL-VERIFIED {c['tool_rule']}]" if c.get("tool_verified") else ""
            lines.append(f"  {c['id']:<6} {at}{c['category'] or c['verdict']}: score "
                         f"{c['weighted_score']:.2f}, share {c['support_share']:.2f} "
                         f"{'ACCEPT' if c['vote_accept'] else 'reject'}{tool}  <- {sup or 'no model'}")
        return "\n".join(lines)
    if event == "judge_ruling":
        lines = [f"\nJudge ({d['judge_model_id']}): "
                 + ("skipped (early finish)" if d["skipped"] else
                    "OVERRIDES the weighted vote" if d["overrides_vote"] else "agrees with the vote")]
        for r in d["rulings"]:
            lines.append(f"  {r['cluster_id']}: {'accept' if r['accept'] else 'reject'}"
                         + (f"  (override) {r['reason']}" if r["overrides_vote"] else ""))
        if d.get("error"):
            lines.append(f"  judge error: {d['error']}")
        return "\n".join(lines)
    if event == "final":
        v = d["verdict"]
        out = [f"\nVERDICT: {v['overall']}  - {v['summary']}"]
        for c in d["confirmed_findings"]:
            out.append(f"  confirmed L{c['line_start']}-{c['line_end']} [{c['category']}] {c['claim']}")
        for c in d["disputed_findings"]:
            out.append(f"  disputed  L{c['line_start']}-{c['line_end']} [{c['category']}] {c['claim']}")
        for x in d["dissent"]:
            out.append(f"  dissent   {x['alias']}: {x['verdict']} ({x['confidence']:.2f})")
        rec = d["recommendation"]
        out.append(f"  recommendation ({rec['confidence']:.2f}): {rec['text']}")
        if d.get("next_step"):
            out.append(f"  next step: {d['next_step']['text']}")
            if d["next_step"].get("patch"):
                out.append(f"    patch: {d['next_step']['patch']}")
        for a in d["agreements"]:
            out.append(f"  agreed    {a['claim']} ({','.join(a['aliases'])})")
        for c in d["clashes"]:
            out.append(f"  clash     {c['claim']}: for {','.join(c['for_side']['aliases'])} / "
                       f"against {','.join(c['against_side']['aliases'])}")
        for b in d["blind_spots"]:
            out.append(f"  blind spot ({b['alias']}): {b['text']}")
        u = d["usage"]
        out.append(f"  {u['calls']} model calls, {u['prompt_tokens'] + u['completion_tokens']} tokens, "
                   f"{u['elapsed_ms'] / 1000:.1f}s")
        return "\n".join(out)
    if event == "error":
        return f"ERROR {d['code']}: {d['message']}"
    return None


async def _review(a) -> int:
    state = _state()
    await state.registry.refresh()
    text = sys.stdin.read() if a.file == "-" else Path(a.file).read_text()
    models = _resolve(state, a.models.split(",")) if a.models else _default_models(state)
    if not models:
        raise SystemExit("no models available. Pull one with Ollama or set QUORUM_ENABLE_FAKE=1")
    judge = _resolve(state, [a.judge])[0] if a.judge else None
    req = RunRequest(pack=a.pack, input=text, model_ids=models, judge_model_id=judge,
                     max_rounds=a.rounds, seed=a.seed, escalate=a.escalate, ground=a.ground,
                     frame=False if a.no_frame else None)
    run_id = uuid.uuid4().hex[:10]
    events: list[dict] = []

    def emit(event, data):
        d = data.model_dump(mode="json")
        events.append({"event": event, "data": d})
        if not a.json and (line := _fmt_event(event, d)):
            print(line, flush=True)

    try:
        await state.debate.run(req, run_id, emit)
    except Exception as e:  # noqa: BLE001
        print(f"error: {getattr(e, 'message', e)}", file=sys.stderr)
        return 1
    finally:
        await state.registry.aclose()
    if a.json:
        print(json.dumps(events, indent=2))
    if a.export:
        run = {"run_id": run_id, "request": req.model_dump(), "status": "done",
               "events": [{"seq": i, "event": e["event"], "data": e["data"]}
                          for i, e in enumerate(events, 1)]}
        out = Path(a.export)
        out.write_text(to_html(run) if out.suffix == ".html" else to_markdown(run))
        print(f"exported: {out}")
    return 0


async def _calibrate(a) -> int:
    state = _state()
    await state.registry.refresh()
    models = _resolve(state, a.models.split(","))
    packs = [a.pack] if a.pack else [p.id for p in state.packs.list()]

    def progress(i, n, msg):
        print(f"\r  {i}/{n}  {msg[:70]:<70}", end="", flush=True)

    out = await calibrate_many(state.debate, models, packs, limit=a.limit, progress=progress)
    print()
    for f in out["failures"]:
        print(f"FAILED {f['model_id']} on {f['pack']}: {f['error']}")
    _print_profiles(state, models)
    await state.registry.aclose()
    return 1 if out["failures"] else 0


def _print_profiles(state: AppState, only: list[str] | None = None) -> None:
    book = WeightBook(state.store.profile_cells(), state.settings.default_weight)
    prof = book.profiles()
    if not prof:
        print("No profiles yet. Run `quorum calibrate`.")
        return
    for m, packs in prof.items():
        if only and m not in only:
            continue
        for p, cats in packs.items():
            print(f"{m}  [{p}]")
            for c, cell in cats.items():
                print(f"    {c:<20} n={cell.n:<3} rate={cell.rate:.2f}  lower={cell.lower:.2f}  "
                      f"weight={cell.weight:.2f}")


async def _eval(a) -> int:
    state = _state()
    await state.registry.refresh()
    models = _resolve(state, a.models.split(","))
    judge = _resolve(state, [a.judge])[0] if a.judge else None
    req = EvalRequest(pack=a.pack, model_ids=models, judge_model_id=judge, max_rounds=a.rounds,
                      limit=a.limit)
    res = await run_eval(state.debate, req,
                         lambda i, n, m: print(f"\r  item {i}/{n} {m:<30}", end="", flush=True))
    print(f"\n\nHeld-out evaluation  pack={res.pack}  items={res.n_items} "
          f"({res.n_positive} positive / {res.n_negative} negative)  judge={res.judge_model_id}")
    iv = lambda x: f"{x.value:.2f} [{x.lo:.2f},{x.hi:.2f}]" if x else "   n/a   "
    print(f"{'#':<2} {'condition':<42} {'precision':<18} {'recall':<18} {'F1':<18} {'FPR':<18}")
    for c in res.conditions:
        if not c.available:
            print(f"{c.id:<2} {c.name:<42} (not available: {c.reason_unavailable})")
            continue
        print(f"{c.id:<2} {c.name:<42} {iv(c.precision):<18} {iv(c.recall):<18} "
              f"{iv(c.f1):<18} {iv(c.fpr):<18}")
    mv = res.debate_movement
    print(f"\nDebate moved a model toward the right answer {mv.toward_correct}x, "
          f"toward the wrong one {mv.toward_wrong}x, no change in correctness {mv.neutral}x.")
    for n in res.notes:
        print(f"note: {n}")
    print(f"saved: {state.settings.home / 'eval' / (res.id + '.csv')}")
    await state.registry.aclose()
    return 0


async def _models(a) -> int:
    state = _state()
    if a.action == "add":
        try:
            info = await state.registry.add(ModelCreate(
                provider=a.provider, tag=a.tag, base_url=a.base_url, api_key_env=a.api_key_env,
                family=a.family, license=a.license, locality=a.locality,
                open_weight=True if a.open_weight else None, calibrate=False))
        except RegistryError as e:
            print(f"error: {e.message}", file=sys.stderr)
            return 1
        print(f"added {info.id} ({info.family}, {info.license}). Run `quorum calibrate --models {info.tag}`")
        return 0
    for m in await state.registry.list():
        print(f"{m.id:<42} {m.family:<14} {m.license:<28} {m.locality:<6} open_weight={m.open_weight!s:<5} "
              f"{m.status}{' - ' + m.status_detail if m.status_detail else ''}")
    await state.registry.aclose()
    return 0


def _packs(a) -> int:
    state = _state()
    if a.action == "validate":
        problems = state.packs.validate(a.pack)
        print("OK" if not problems else "\n".join(problems))
        return 1 if problems else 0
    for p in state.packs.list():
        print(f"{p.id:<20} {p.modes}  cal={p.corpus_counts.get('calibration')} "
              f"heldout={p.corpus_counts.get('heldout')}  categories={p.categories}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="quorum")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve")
    s.add_argument("--host", default=None)
    s.add_argument("--port", type=int, default=None)

    r = sub.add_parser("review", help="debate a diff or claim and print the transcript")
    r.add_argument("file", help="path to a diff/code/claim file, or - for stdin")
    r.add_argument("--pack", default="code-review")
    r.add_argument("--models", help="comma-separated ids or tags (default: three families)")
    r.add_argument("--judge")
    r.add_argument("--rounds", type=int, default=2)
    r.add_argument("--seed", type=int)
    r.add_argument("--json", action="store_true")
    r.add_argument("--escalate", choices=["auto", "always"], default="always",
                   help="auto: two models first, full debate only if they disagree")
    r.add_argument("--ground", action="store_true",
                   help="run Semgrep over the input; a tool-confirmed finding outranks the vote")
    r.add_argument("--no-frame", action="store_true", help="skip the neutral framing call")
    r.add_argument("--export", help="write a transcript to this .md or .html file")

    c = sub.add_parser("calibrate")
    c.add_argument("--models", required=True)
    c.add_argument("--pack")
    c.add_argument("--limit", type=int)

    e = sub.add_parser("eval")
    e.add_argument("--pack", required=True)
    e.add_argument("--models", required=True)
    e.add_argument("--judge")
    e.add_argument("--rounds", type=int, default=2)
    e.add_argument("--limit", type=int)

    sub.add_parser("profiles")

    m = sub.add_parser("models")
    m.add_argument("action", nargs="?", default="list", choices=["list", "add"])
    m.add_argument("--provider", choices=["ollama", "openai_compat", "digitalocean"])
    m.add_argument("--tag")
    m.add_argument("--base-url", dest="base_url")
    m.add_argument("--api-key-env", dest="api_key_env")
    m.add_argument("--family")
    m.add_argument("--locality", choices=["local", "hosted"])
    m.add_argument("--license")
    m.add_argument("--open-weight", action="store_true", dest="open_weight")

    p = sub.add_parser("packs")
    p.add_argument("action", nargs="?", default="list", choices=["list", "validate"])
    p.add_argument("pack", nargs="?")

    a = ap.parse_args(argv)
    if a.cmd == "serve":
        import uvicorn

        st = Settings.from_env()
        uvicorn.run("quorum.api:create_app", factory=True, host=a.host or st.host,
                    port=a.port or st.port, log_level="info")
        return 0
    if a.cmd == "packs":
        return _packs(a)
    if a.cmd == "profiles":
        _print_profiles(_state())
        return 0
    if a.cmd == "models" and a.action == "add" and not (a.provider and a.tag):
        ap.error("models add needs --provider and --tag")
    handler = {"review": _review, "calibrate": _calibrate, "eval": _eval, "models": _models}[a.cmd]
    return asyncio.run(handler(a))


if __name__ == "__main__":
    raise SystemExit(main())
