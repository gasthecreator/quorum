"""Run export (B14): a Markdown transcript and a self-contained HTML report.

The alias mapping is revealed here (debaters never saw it). Everything that came from a model or
from the input is escaped: both formats are safe to open or paste without running anything.
"""

from __future__ import annotations

import html
import re
from typing import Any


def _events(run: dict, name: str) -> list[dict]:
    return [e["data"] for e in run["events"] if e["event"] == name]


def _first(run: dict, name: str) -> dict | None:
    got = _events(run, name)
    return got[0] if got else None


def _fence(text: str) -> str:
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _md(s: str) -> str:
    """Escape model text for Markdown so it cannot inject HTML or headings."""
    return html.escape(s or "", quote=False).replace("\r", "").strip()


def _quote(s: str) -> str:
    return "\n".join("> " + ln for ln in _md(s).split("\n")) if s.strip() else ""


def _pct(x: float) -> str:
    return f"{x:.2f}"


def _model(run: dict, alias: str, started: dict | None) -> str:
    if not started:
        return alias
    for mid, a in started["aliases"].items():
        if a == alias:
            return mid
    return alias


def _lines(c: dict) -> str:
    return f"lines {c['line_start']}-{c['line_end']}" if c["kind"] == "finding" else c.get("verdict", "")


def to_markdown(run: dict) -> str:
    started = _first(run, "run_started")
    out: list[str] = [f"# Quorum run {run['run_id']}", ""]
    req = run["request"]
    if started:
        out += [f"- Pack: `{started['pack']}`  ·  rounds: up to {started['max_rounds']}  ·  "
                f"escalate: `{started['escalate']}`  ·  framer: {'on' if started['frame'] else 'off'}",
                f"- Judge: `{started['judge_model_id']}`", "- Panel (alias → model):"]
        out += [f"  - **{a}** → `{m}`" for m, a in sorted(started["aliases"].items(), key=lambda kv: kv[1])]
        if started["input_flags"]:
            out += ["", f"> ⚠ The input contains text that looks aimed at AI models "
                        f"({', '.join(started['input_flags'])}). It was treated as data."]
    fence = _fence(req["input"])
    out += ["", "## Input", "", f"{fence}\n{req['input'].rstrip()}\n{fence}"]
    framed = _first(run, "framed")
    if framed:
        out += ["", "## Neutral framing", "",
                _quote(framed["restatement"]) if framed["restatement"]
                else f"_Framer failed: {_md(framed.get('error') or 'no output')}_"]

    rnd = None
    for e in run["events"]:
        ev, d = e["event"], e["data"]
        if ev == "round_started":
            rnd = d["round"]
            out += ["", f"## Round {rnd}" + (" (blind)" if rnd == 0 else "")]
        elif ev == "escalated":
            out += ["", f"**Escalated** ({d['reason']}): {_md(d['detail'])}"]
        elif ev == "model_message":
            who = f"**{d['alias']}** (`{d['model_id']}`)"
            p = d["position"]
            flip = f" — **changed**: {_md(d['changed_reason'])}" if d["changed"] else (
                f" — _{_md(d['changed_reason'])}_" if d["changed_reason"] else "")
            err = f" — **error**: {_md(d['error'])}" if d["error"] else ""
            out += ["", f"### {d['alias']}: {p['verdict']} ({_pct(p['confidence'])})", "",
                    f"{who}{flip}{err}"]
            for f in d["findings"]:
                out.append(f"- lines {f['line_start']}-{f['line_end']} `{_md(f['category'])}` "
                           f"({f['severity']}): {_md(f['claim'])}")
            if d["text"]:
                out += ["", _quote(d["text"])]
            c = d.get("critique")
            if c:
                out += ["", "Critique of the others:",
                        f"- Strongest: {_md(c['strongest'])}",
                        f"- Biggest blind spot: {_md(c['biggest_blind_spot'])}",
                        f"- Missed by all: {_md(c['missed_by_all'])}"]
        elif ev == "round_finished":
            out += ["", f"_Agreement after round {d['round']}: {_pct(d['agreement'])}_"]
        elif ev == "stopped":
            out += ["", f"**Stopped:** {d['reason']} (round {d['round']})"]
        elif ev == "finished_early":
            out += ["", f"**Finished early:** {_md(d['reason'])} "
                        f"({d['calls_saved']} model calls saved)"]
        elif ev == "tally":
            out += ["", "## Tally", "", "| Cluster | What | Category | Backed by | Score | Share | Vote |",
                    "| --- | --- | --- | --- | --- | --- | --- |"]
            for c in d["clusters"]:
                sup = ", ".join(f"{s['alias']} (w {_pct(s['weight'])}, {s['weight_source']})"
                                for s in c["supporters"])
                out.append(f"| {c['id']} | {_lines(c)}: {_md(c['claim']).replace('|', '/')} | "
                           f"{_md(c['category'])} | {sup} | {_pct(c['weighted_score'])} | "
                           f"{_pct(c['support_share'])} | {'accept' if c['vote_accept'] else 'reject'} |")
        elif ev == "judge_ruling":
            out += ["", "## Judge", "",
                    f"`{d['judge_model_id']}` " + ("skipped (early finish)." if d["skipped"] else (
                        "**overrode the weighted vote.**" if d["overrides_vote"]
                        else "agreed with the weighted vote."))]
            if d["overrides_vote"] and d["reason"]:
                out += ["", _quote(d["reason"])]
            for r in d["rulings"]:
                note = f" — override: {_md(r['reason'])}" if r["overrides_vote"] else ""
                out.append(f"- {r['cluster_id']}: {'accept' if r['accept'] else 'reject'}{note}")
        elif ev == "final":
            out += _final_md(d)
        elif ev == "error":
            out += ["", f"## Error `{d['code']}`", "", _md(d["message"])]
    return "\n".join(out).rstrip() + "\n"


def _final_md(d: dict) -> list[str]:
    v, rec = d["verdict"], d["recommendation"]
    out = ["", "## Verdict", "", f"**{v['overall']}** — {_md(v['summary'])}", "",
           f"**Recommendation** ({_pct(rec['confidence'])} confidence): {_md(rec['text'])}"]
    ns = d.get("next_step")
    if ns:
        out += ["", f"**Next step:** {_md(ns['text'])}"]
        if ns.get("patch"):
            fence = _fence(ns["patch"])
            out += ["", f"{fence}\n{ns['patch'].rstrip()}\n{fence}"]
    for title, key in (("Confirmed", "confirmed_findings"), ("Disputed", "disputed_findings")):
        if d[key]:
            out += ["", f"### {title}"]
            out += [f"- {_lines(c)} `{_md(c['category'])}`: {_md(c['claim'])}" for c in d[key]]
    if d["agreements"]:
        out += ["", "### Where everyone agreed"]
        out += [f"- {_md(a['claim'])} ({', '.join(a['aliases'])})" for a in d["agreements"]]
    if d["clashes"]:
        out += ["", "### Clashes"]
        for c in d["clashes"]:
            out += [f"- {_md(c['claim'])}",
                    f"  - For ({', '.join(c['for_side']['aliases'])}): {_md(c['for_side']['argument'])}",
                    f"  - Against ({', '.join(c['against_side']['aliases'])}): "
                    f"{_md(c['against_side']['argument'])}"]
    if d["blind_spots"]:
        out += ["", "### Blind spots"]
        out += [f"- {b['alias']} ({b['kind'].replace('_', ' ')}): {_md(b['text'])}"
                for b in d["blind_spots"]]
    if d["dissent"]:
        out += ["", "### Dissent"]
        out += [f"- {x['alias']}: {x['verdict']} ({_pct(x['confidence'])})" for x in d["dissent"]]
    u = d["usage"]
    out += ["", f"_{u['calls']} model calls · {u['prompt_tokens'] + u['completion_tokens']} tokens · "
                f"{u['elapsed_ms'] / 1000:.1f}s_"]
    return out


# --------------------------------------------------------------------------------------- HTML

CSS = """
:root{color-scheme:light dark;--bg:#fff;--fg:#1b1f24;--mut:#59636e;--line:#d0d7de;--card:#f6f8fa;
--A:#0969da;--B:#8250df;--C:#1a7f37;--D:#bf8700;--E:#cf222e;--F:#0a7ea4;--ok:#1a7f37;--bad:#cf222e}
@media(prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--mut:#8d96a0;--line:#30363d;
--card:#161b22;--A:#58a6ff;--B:#bc8cff;--C:#3fb950;--D:#d29922;--E:#ff7b72;--F:#39c5cf;--ok:#3fb950;--bad:#ff7b72}}
body{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;max-width:860px;margin:2rem auto;padding:0 1rem}
h1,h2,h3{line-height:1.2}h2{border-bottom:1px solid var(--line);padding-bottom:.3rem;margin-top:2rem}
pre{background:var(--card);border:1px solid var(--line);padding:.75rem;overflow:auto;border-radius:6px}
code{background:var(--card);padding:.1em .3em;border-radius:4px}pre code{background:none;padding:0}
.msg{border-left:4px solid var(--line);background:var(--card);padding:.5rem .9rem;margin:.75rem 0;border-radius:0 6px 6px 0}
.A{border-color:var(--A)}.B{border-color:var(--B)}.C{border-color:var(--C)}.D{border-color:var(--D)}.E{border-color:var(--E)}.F{border-color:var(--F)}
.badge{display:inline-block;font-size:12px;padding:0 .5em;border-radius:99px;border:1px solid var(--line);margin-left:.4em}
.flip{color:var(--bad);border-color:var(--bad)}.ok{color:var(--ok);border-color:var(--ok)}
table{border-collapse:collapse;width:100%}td,th{border:1px solid var(--line);padding:.3rem .5rem;text-align:left;vertical-align:top}
.mut{color:var(--mut)}.warn{border:1px solid var(--bad);padding:.5rem;border-radius:6px}
""".strip()


def _h(s: Any) -> str:
    return html.escape(str(s if s is not None else ""))


def to_html(run: dict) -> str:
    started = _first(run, "run_started")
    req = run["request"]
    p: list[str] = []
    p.append(f"<h1>Quorum run {_h(run['run_id'])}</h1>")
    if started:
        p.append(f"<p class=mut>Pack <code>{_h(started['pack'])}</code> · up to {started['max_rounds']} "
                 f"rounds · escalate <code>{_h(started['escalate'])}</code> · framer "
                 f"{'on' if started['frame'] else 'off'} · judge <code>{_h(started['judge_model_id'])}</code></p>")
        p.append("<table><tr><th>Alias</th><th>Model</th></tr>" + "".join(
            f"<tr><td>{_h(a)}</td><td><code>{_h(m)}</code></td></tr>"
            for m, a in sorted(started["aliases"].items(), key=lambda kv: kv[1])) + "</table>")
        if started["input_flags"]:
            p.append(f"<p class=warn>The input contains text that looks aimed at AI models "
                     f"({_h(', '.join(started['input_flags']))}). It was treated as data.</p>")
    p.append(f"<h2>Input</h2><pre><code>{_h(req['input'])}</code></pre>")
    framed = _first(run, "framed")
    if framed:
        p.append("<h2>Neutral framing</h2><p>" + (_h(framed["restatement"]) if framed["restatement"]
                 else f"<em>Framer failed: {_h(framed.get('error') or 'no output')}</em>") + "</p>")
    for e in run["events"]:
        ev, d = e["event"], e["data"]
        if ev == "round_started":
            p.append(f"<h2>Round {d['round']}{' (blind)' if d['round'] == 0 else ''}</h2>")
        elif ev == "escalated":
            p.append(f"<p><strong>Escalated</strong> ({_h(d['reason'])}): {_h(d['detail'])}</p>")
        elif ev == "model_message":
            pos = d["position"]
            badges = ""
            if d["changed"]:
                badges += "<span class='badge flip'>changed</span>"
            if d["error"]:
                badges += "<span class='badge flip'>error</span>"
            body = [f"<strong>{_h(d['alias'])}</strong> <code>{_h(d['model_id'])}</code> — "
                    f"{_h(pos['verdict'])} ({_pct(pos['confidence'])}){badges}"]
            if d["changed_reason"]:
                body.append(f"<div class=mut>{_h(d['changed_reason'])}</div>")
            if d["error"]:
                body.append(f"<div>{_h(d['error'])}</div>")
            if d["findings"]:
                body.append("<ul>" + "".join(
                    f"<li>lines {f['line_start']}-{f['line_end']} <code>{_h(f['category'])}</code> "
                    f"({_h(f['severity'])}): {_h(f['claim'])}</li>" for f in d["findings"]) + "</ul>")
            if d["text"]:
                body.append(f"<p>{_h(d['text'])}</p>")
            c = d.get("critique")
            if c:
                body.append("<div class=mut>Critique — strongest: " + _h(c["strongest"])
                            + "; blind spot: " + _h(c["biggest_blind_spot"])
                            + "; missed by all: " + _h(c["missed_by_all"]) + "</div>")
            cls = re.sub(r"[^A-Z]", "", d["alias"])[:1]
            p.append(f"<div class='msg {cls}'>{''.join(body)}</div>")
        elif ev == "round_finished":
            p.append(f"<p class=mut>Agreement after round {d['round']}: {_pct(d['agreement'])}</p>")
        elif ev == "stopped":
            p.append(f"<p><strong>Stopped:</strong> {_h(d['reason'])} (round {d['round']})</p>")
        elif ev == "finished_early":
            p.append(f"<p><strong>Finished early:</strong> {_h(d['reason'])} "
                     f"({d['calls_saved']} model calls saved)</p>")
        elif ev == "tally":
            rows = "".join(
                f"<tr><td>{_h(c['id'])}</td><td>{_h(_lines(c))}: {_h(c['claim'])}</td>"
                f"<td>{_h(c['category'])}</td><td>" + ", ".join(
                    f"{_h(s['alias'])} (w {_pct(s['weight'])}, {_h(s['weight_source'])})"
                    for s in c["supporters"]) + f"</td><td>{_pct(c['weighted_score'])}</td>"
                f"<td>{_pct(c['support_share'])}</td><td>{'accept' if c['vote_accept'] else 'reject'}</td></tr>"
                for c in d["clusters"])
            p.append("<h2>Tally</h2><table><tr><th>#</th><th>What</th><th>Category</th><th>Backed by</th>"
                     f"<th>Score</th><th>Share</th><th>Vote</th></tr>{rows}</table>")
        elif ev == "judge_ruling":
            verdict = ("skipped (early finish)" if d["skipped"] else (
                "<strong>overrode the weighted vote</strong>" if d["overrides_vote"]
                else "agreed with the weighted vote"))
            p.append(f"<h2>Judge</h2><p><code>{_h(d['judge_model_id'])}</code> {verdict}.</p>")
            if d["overrides_vote"] and d["reason"]:
                p.append(f"<p>{_h(d['reason'])}</p>")
            p.append("<ul>" + "".join(
                f"<li>{_h(r['cluster_id'])}: {'accept' if r['accept'] else 'reject'}"
                + (f" — override: {_h(r['reason'])}" if r["overrides_vote"] else "") + "</li>"
                for r in d["rulings"]) + "</ul>")
        elif ev == "final":
            p.append(_final_html(d))
        elif ev == "error":
            p.append(f"<h2>Error <code>{_h(d['code'])}</code></h2><p>{_h(d['message'])}</p>")
    title = _h(f"Quorum run {run['run_id']}")
    return ("<!doctype html><html lang=en><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{title}</title><style>{CSS}</style></head><body>{''.join(p)}</body></html>")


def _final_html(d: dict) -> str:
    v, rec = d["verdict"], d["recommendation"]
    h = [f"<h2>Verdict</h2><p><strong>{_h(v['overall'])}</strong> — {_h(v['summary'])}</p>",
         f"<p><strong>Recommendation</strong> ({_pct(rec['confidence'])} confidence): {_h(rec['text'])}</p>"]
    ns = d.get("next_step")
    if ns:
        h.append(f"<p><strong>Next step:</strong> {_h(ns['text'])}</p>")
        if ns.get("patch"):
            h.append(f"<pre><code>{_h(ns['patch'])}</code></pre>")
    for title, key in (("Confirmed", "confirmed_findings"), ("Disputed", "disputed_findings")):
        if d[key]:
            h.append(f"<h3>{title}</h3><ul>" + "".join(
                f"<li>{_h(_lines(c))} <code>{_h(c['category'])}</code>: {_h(c['claim'])}</li>"
                for c in d[key]) + "</ul>")
    if d["agreements"]:
        h.append("<h3>Where everyone agreed</h3><ul>" + "".join(
            f"<li>{_h(a['claim'])} ({_h(', '.join(a['aliases']))})</li>" for a in d["agreements"]) + "</ul>")
    if d["clashes"]:
        h.append("<h3>Clashes</h3>" + "".join(
            f"<p>{_h(c['claim'])}<br>For ({_h(', '.join(c['for_side']['aliases']))}): "
            f"{_h(c['for_side']['argument'])}<br>Against ({_h(', '.join(c['against_side']['aliases']))}): "
            f"{_h(c['against_side']['argument'])}</p>" for c in d["clashes"]))
    if d["blind_spots"]:
        h.append("<h3>Blind spots</h3><ul>" + "".join(
            f"<li>{_h(b['alias'])} ({_h(b['kind'].replace('_', ' '))}): {_h(b['text'])}</li>"
            for b in d["blind_spots"]) + "</ul>")
    if d["dissent"]:
        h.append("<h3>Dissent</h3><ul>" + "".join(
            f"<li>{_h(x['alias'])}: {_h(x['verdict'])} ({_pct(x['confidence'])})</li>"
            for x in d["dissent"]) + "</ul>")
    u = d["usage"]
    h.append(f"<p class=mut>{u['calls']} model calls · {u['prompt_tokens'] + u['completion_tokens']} "
             f"tokens · {u['elapsed_ms'] / 1000:.1f}s</p>")
    return "".join(h)
