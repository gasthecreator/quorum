"""Tool grounding (B10): run a static analyser over the code under review.

A tool-confirmed finding outranks any vote. Because of that, the rules in `rules/python.yml` favour
precision over recall, and the analyser only ever parses the input; it never executes it.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Protocol

from .schemas import ToolFinding

RULES = Path(__file__).parent / "rules" / "python.yml"


class Grounder(Protocol):
    name: str

    def available(self) -> bool: ...

    async def check(self, text: str) -> tuple[list[ToolFinding], str | None]:
        """Return (findings, error). An error means the tool could not run, not that code is clean."""
        ...


def to_source(text: str) -> tuple[str, str | None]:
    """Make analysable source whose line numbers equal the pasted text's line numbers.

    Models cite lines of what was pasted. For a unified diff we blank headers and removed lines and
    strip the +/space marker, so line N of the result is line N of the paste.
    Returns (source, unsupported_reason).
    """
    lines = text.replace("\r\n", "\n").split("\n")
    is_diff = any(ln.startswith(("@@ ", "diff --git ")) for ln in lines)
    if not is_diff:
        return text, None
    out: list[str] = []
    in_hunk = False
    path = None
    for ln in lines:
        if ln.startswith("+++ "):
            path = re.sub(r"^[ab]/", "", ln[4:].split("\t")[0].strip())
            out.append("")
            in_hunk = False
        elif ln.startswith(("--- ", "diff --git", "index ", "new file", "deleted file")):
            out.append("")
            in_hunk = False
        elif ln.startswith("@@"):
            out.append("")
            in_hunk = True
        elif in_hunk and ln.startswith("+") or in_hunk and ln.startswith(" "):
            out.append(ln[1:])
        else:  # removed lines, "\ No newline", anything else
            out.append("")
    if path and not path.endswith(".py"):
        return "", f"no rules for {Path(path).suffix or 'this file type'} (Python only)"
    return "\n".join(out), None


def _semgrep_binary() -> str | None:
    beside = Path(sys.executable).parent / "semgrep"
    if beside.is_file():
        return str(beside)
    return shutil.which("semgrep")


class SemgrepGrounder:
    name = "semgrep"

    def __init__(self, binary: str | None = None, rules: Path = RULES, timeout_s: float = 60.0):
        self.binary = binary or _semgrep_binary()
        self.rules = rules
        self.timeout_s = timeout_s

    def available(self) -> bool:
        return bool(self.binary) and os.access(self.binary, os.X_OK) and self.rules.is_file()

    async def check(self, text: str) -> tuple[list[ToolFinding], str | None]:
        return (await self.check_many([text]))[0]

    async def check_many(self, texts: list[str]) -> list[tuple[list[ToolFinding], str | None]]:
        """Analyse several inputs in one Semgrep run (it starts slowly). One result per input."""
        if not self.available():
            return [([], "semgrep is not installed (pip install semgrep)")] * len(texts)
        results: list[tuple[list[ToolFinding], str | None]] = [([], None)] * len(texts)
        todo: dict[str, int] = {}
        with tempfile.TemporaryDirectory(prefix="quorum-ground-") as d:
            for i, text in enumerate(texts):
                source, unsupported = to_source(text)
                if unsupported:
                    results[i] = ([], unsupported)
                    continue
                path = Path(d) / f"snippet_{i}.py"
                path.write_text(source)
                todo[str(path)] = i
            if not todo:
                return results
            env = {**os.environ, "SEMGREP_SEND_METRICS": "off", "SEMGREP_ENABLE_VERSION_CHECK": "0",
                   "SEMGREP_SETTINGS_FILE": str(Path(d) / "settings.yml")}
            proc = await asyncio.create_subprocess_exec(
                self.binary, "--config", str(self.rules), "--json", "--metrics=off",
                "--disable-version-check", "--quiet", "--no-git-ignore", "--timeout", "20",
                *todo, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
            try:
                out, err = await asyncio.wait_for(proc.communicate(), self.timeout_s)
            except TimeoutError:
                proc.kill()
                msg = f"semgrep timed out after {self.timeout_s:.0f}s"
                for i in todo.values():
                    results[i] = ([], msg)
                return results
        if proc.returncode not in (0, 1):
            msg = f"semgrep failed: {err.decode(errors='replace').strip()[:200]}"
            for i in todo.values():
                results[i] = ([], msg)
            return results
        try:
            data = json.loads(out)
        except ValueError:
            for i in todo.values():
                results[i] = ([], "semgrep returned unreadable output")
            return results
        per: dict[int, dict[tuple[str, int], ToolFinding]] = {i: {} for i in todo.values()}
        for r in data.get("results", []):
            i = todo.get(str(Path(r["path"]).resolve())) if str(Path(r["path"]).resolve()) in todo \
                else todo.get(r["path"])
            if i is None:
                continue
            extra = r.get("extra", {})
            cat = (extra.get("metadata") or {}).get("category", "other")
            start, end = r["start"]["line"], r["end"]["line"]
            rule = r["check_id"].rsplit(".", 1)[-1]
            per[i].setdefault((rule, start), ToolFinding(
                tool=self.name, rule_id=rule, category=cat, file="", line_start=start,
                line_end=end, claim=str(extra.get("message", rule))[:300]))
        for i, found in per.items():
            results[i] = (sorted(found.values(), key=lambda f: f.line_start), None)
        return results


def default_grounder() -> Grounder | None:
    g = SemgrepGrounder()
    return g if g.available() else None


async def timed_check(g: Grounder, text: str) -> tuple[list[ToolFinding], str | None, int]:
    t0 = time.monotonic()
    findings, err = await g.check(text)
    return findings, err, int((time.monotonic() - t0) * 1000)
