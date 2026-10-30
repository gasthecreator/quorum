"""Simulated models for tests and offline development (QUORUM_ENABLE_FAKE=1).

These are NOT language models. They read the same prompts a real model would and answer with
rule-based logic so the whole pipeline (debate, tally, judge, calibration, eval) can run on a
machine with no inference server. Never use them for numbers you intend to publish.

Tags (model ids are fake:<tag>):
  sharp     finds all four dev-pack bug types; holds position, adopts what two peers agree on
  sloppy    misses unbounded queries, false-alarms on every range(len(..)), obeys injected
                 instructions, and follows its peers when they disagree with it
  stubborn  like sharp but blind to unbounded queries and never changes its mind
  judge-skeptic  judge that rejects single-supporter clusters and accepts multi-supporter ones
  broken    always returns text that is not JSON
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from typing import ClassVar

from ..schemas import DebaterOutput
from .base import ChatMessage, Completion

CLAIM_TRUTH = {
    "Mount Kilimanjaro is in Tanzania.": True,
    "The Danube flows into the Baltic Sea.": False,
    "Water boils at a lower temperature at higher altitude.": True,
    "Sound travels faster in air than in water.": False,
    "The Berlin Wall fell in 1989.": True,
    "The Magna Carta was sealed in 1415.": False,
    "Lake Victoria borders Kenya, Uganda and Tanzania.": True,
    "The capital of Australia is Sydney.": False,
    "Helium is the lightest element.": False,
    "DNA is a double helix.": True,
    "The Apollo 11 landing took place in 1969.": True,
    "World War I began in 1916.": False,
}

SQL_KW = re.compile(r"\b(SELECT|DELETE|INSERT|UPDATE)\b")
UNSAFE_SQL = re.compile(r"(\"\s*\+|'\s*\+|\+\s*\w+\s*\+|\bf[\"'].*\{|\.format\()")


def _pct(*parts: str) -> int:
    return int(hashlib.sha256("|".join(parts).encode()).hexdigest(), 16) % 100


def _between(text: str, start: str, end: str) -> str | None:
    m = re.search(re.escape(start) + r"\n(.*?)\n" + re.escape(end), text, re.DOTALL)
    return m.group(1) if m else None


class FakeProvider:
    def __init__(self, tag: str, delay_s: float = 0.0):
        self.tag = tag
        self.delay_s = delay_s
        self.kind = tag

    async def aclose(self) -> None:  # pragma: no cover - nothing to close
        return None

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        json_schema: dict | None = None,
        max_tokens: int = 700,
        temperature: float = 0.2,
        timeout: float = 180.0,
    ) -> Completion:
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        system, user = messages[0]["content"], messages[-1]["content"]
        if self.kind == "broken":
            return Completion("I think the code looks fine to me!", 10, 8)
        if "You restate inputs neutrally" in system:
            out = self._frame(user)
        elif "You are the judge" in system:
            out = self._judge(user)
        else:
            out = self._debater(system, user)
        text = json.dumps(out)
        return Completion(text, prompt_tokens=len(system + user) // 4, completion_tokens=len(text) // 4)

    # ------------------------------------------------------------------ framer

    def _frame(self, user: str) -> dict:
        m = re.search(r"=== INPUT (\w+) ===\n(.*?)\n=== END INPUT", user, re.DOTALL)
        body = m.group(2) if m else ""
        if not re.search(r"^\s*\d+ \| ", body, re.MULTILINE):
            return {"restatement": f"Is the following statement accurate? {body.strip()[:300]}"}
        code = [re.sub(r"^\s*\d+ \| ?", "", ln) for ln in body.splitlines()]
        sig = next((c.strip() for c in code if c.strip() and not c.strip().startswith("#")), "")
        return {"restatement": f"A code snippet of {len(code)} lines, starting with `{sig[:80]}`. "
                               "Review it for defects."}

    # ------------------------------------------------------------------ judge

    def _judge(self, user: str) -> dict:
        m = re.search(r"=== TALLY (\w+) ===\n(.*?)\n=== END TALLY", user, re.DOTALL)
        tally = json.loads(m.group(2)) if m else {"clusters": []}
        rulings = []
        for c in tally["clusters"]:
            n = len(c["supporters"])
            if self.kind == "judge-skeptic":
                if n == 1 and c["vote_accept"]:
                    rulings.append({"cluster_id": c["id"], "accept": False,
                                    "reason": "Only one panelist raised this and the input does not make it clear."})
                    continue
                if n >= 2 and not c["vote_accept"]:
                    rulings.append({"cluster_id": c["id"], "accept": True,
                                    "reason": "Several independent panelists agree; the low-weight vote undercounts it."})
                    continue
            rulings.append({"cluster_id": c["id"], "accept": bool(c["vote_accept"]), "reason": ""})
        accepted = [c for c in tally["clusters"] if c["vote_accept"]]
        if not accepted:
            rec = "No change needed."
        elif accepted[0]["kind"] == "verdict":
            rec = f"Treat the claim as {accepted[0]['verdict']}."
        else:
            rec = f"Fix the {accepted[0]['category']} problem first."
        return {"rulings": rulings, "reason": "Followed the weighted vote unless noted.",
                "recommendation": rec}

    # ------------------------------------------------------------------ debaters

    def _debater(self, system: str, user: str) -> dict:
        claim_mode = "fact-checkers" in system
        rm = re.search(r"^Round (\d+)", user, re.MULTILINE)
        rnd = int(rm.group(1)) if rm else 0
        nonce_m = re.search(r"=== INPUT (\w+) ===", user)
        nonce = nonce_m.group(1) if nonce_m else ""
        body = _between(user, f"=== INPUT {nonce} ===", f"=== END INPUT {nonce} ===") or ""
        named = [(a, json.loads(b)) for a, b in re.findall(
            rf"=== PEER (\w) {nonce} ===\n(.*?)\n=== END PEER", user, re.DOTALL)] if rnd else []
        peers = [p for _, p in named]
        out = self._claim(body, peers, rnd) if claim_mode else self._review(body, peers, rnd)
        if rnd and named:
            out["critique"] = self._critique(named)
        return DebaterOutput.model_validate(out).model_dump()

    def _review(self, numbered: str, peers: list[dict], rnd: int) -> dict:
        lines = [(int(a), b) for a, b in re.findall(r"^\s*(\d+) \| ?(.*)$", numbered, re.MULTILINE)]
        found = self._detect(lines)
        findings = [self._finding(*f) for f in found]
        changed, reason = False, ""
        if rnd and self.kind != "stubborn":
            peer_f = [f for p in peers for f in p["findings"]]
            counts: dict[int, int] = {}
            for f in peer_f:
                counts[f["line_start"]] = counts.get(f["line_start"], 0) + 1
            need = 1 if self.kind == "sloppy" else 2
            have = {f["line_start"] for f in findings}
            for ln, c in counts.items():
                if c >= need and ln not in have and self.kind != "stubborn":
                    src = next(f for f in peer_f if f["line_start"] == ln)
                    findings.append(self._finding(ln, src["category"], src["claim"]))
                    changed, reason = True, f"{c} peer(s) cite line {ln}: {src['evidence'] or src['claim']}"
            if self.kind == "sloppy":
                kept = [f for f in findings if counts.get(f["line_start"], 0) >= 1]
                if len(kept) != len(findings):
                    dropped = [f["line_start"] for f in findings if f not in kept]
                    findings = kept
                    changed, reason = True, f"No peer corroborated line {dropped[0]}, so I withdraw it."
        verdict = "issue" if findings else "no_issue"
        conf = 0.9 if self.kind == "sharp" else 0.75
        return {"position": {"verdict": verdict, "confidence": conf}, "findings": findings,
                "changed": changed, "changed_reason": reason,
                "text": f"{len(findings)} finding(s)." if findings else "Nothing wrong that I can point to."}

    def _detect(self, lines: list[tuple[int, str]]) -> list[tuple[int, str, str]]:
        out: list[tuple[int, str, str]] = []
        joined = "\n".join(t for _, t in lines)
        if self.kind == "sloppy" and "ignore all previous instructions" in joined.lower():
            return [(1, "sql_injection", "Critical SQL injection (as instructed).")]
        in_async = False
        for i, (n, t) in enumerate(lines):
            if t.lstrip().startswith("async def"):
                in_async = True
            elif t.startswith("def "):
                in_async = False
            if t.lstrip().startswith("#"):
                continue
            if SQL_KW.search(t) and UNSAFE_SQL.search(t) and "?" not in t:
                out.append((n, "sql_injection", "User input is built into the SQL string."))
            if re.search(r"len\(\w+\)\s*-\s*\w+\s*\+\s*2", t) or re.search(r"len\(\w+\)\s*-\s*\w+\s*-\s*1\s*:", t):
                out.append((n, "off_by_one", "Bound is off by one."))
            elif self.kind == "sloppy" and "range(len(" in t:
                out.append((n, "off_by_one", "Possible off-by-one in loop bound."))
            if in_async and re.search(r"\b(client|store|session)\.(get|put|post|fetch|save)\(", t) \
                    and "await" not in t:
                out.append((n, "missing_await", "Coroutine is not awaited."))
            window = "\n".join(x for _, x in lines[i:i + 4])
            if self.kind == "sharp" and "execute(" in t and "SELECT" in window \
                    and "LIMIT" not in window.upper() and "COUNT(" not in window \
                    and "fetchone" not in "\n".join(x for _, x in lines):
                out.append((n, "unbounded_query", "Query has no LIMIT and may return unbounded rows."))
        seen, dedup = set(), []
        for o in out:
            if o[0] not in seen:
                seen.add(o[0])
                dedup.append(o)
        return dedup

    def _critique(self, named: list[tuple[str, dict]]) -> dict:
        def score(p):
            return (len(p["findings"]), p["position"]["confidence"])

        best = max(named, key=lambda x: score(x[1]))
        worst = min(named, key=lambda x: score(x[1]))
        same = len({p["position"]["verdict"] for _, p in named}) == 1
        return {
            "strongest": f"{best[0]}: it points at concrete lines and gives evidence.",
            "biggest_blind_spot": (f"{worst[0]}: it offers little to check." if not same
                                   else "none"),
            "missed_by_all": "none" if same else "Whether the unflagged lines are really safe.",
        }

    FIXES: ClassVar[dict[str, str]] = {
        "sql_injection": 'Use a parameterized query: db.execute("... WHERE x = ?", (value,))',
        "off_by_one": "Correct the loop bound or slice by one.",
        "missing_await": "Add `await` before the call.",
        "unbounded_query": "Add a LIMIT clause and cap the page size.",
    }

    def _finding(self, line: int, category: str, claim: str) -> dict:
        return {"category": category, "file": "", "line_start": line, "line_end": line,
                "severity": "high", "claim": claim, "evidence": f"Line {line}: {claim}",
                "fix": self.FIXES.get(category, "")}

    def _claim(self, text: str, peers: list[dict], rnd: int) -> dict:
        claim = text.strip()
        truth = CLAIM_TRUTH.get(claim)
        if truth is None:
            verdict, conf = "unsure", 0.3
        else:
            acc = {"sharp": 100, "sloppy": 55, "stubborn": 70}.get(self.kind, 60)
            right = _pct(self.kind, claim) < acc
            verdict, conf = str(truth == right).lower(), (0.9 if self.kind == "sharp" else 0.7)
        changed, reason = False, ""
        if rnd and self.kind == "sloppy" and peers:
            votes = [p["position"]["verdict"] for p in peers]
            top = max(set(votes), key=votes.count)
            if votes.count(top) == len(votes) and top != verdict and top in ("true", "false"):
                changed, reason, verdict = True, f"All {len(votes)} peers independently say {top}.", top
        return {"position": {"verdict": verdict, "confidence": conf}, "findings": [],
                "changed": changed, "changed_reason": reason, "text": f"I judge this {verdict}."}
