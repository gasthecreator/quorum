"""Runner (B3): parallel model calls that return validated JSON, with timeouts and retries."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field

from pydantic import BaseModel, ValidationError

from .config import Settings
from .providers import LLMProvider, ProviderError
from .schemas import DebaterOutput, FramerOutput, JudgeOutput


@dataclass
class CallResult:
    output: BaseModel | None = None
    error: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    attempts: int = 0
    raw: str = ""
    notes: list[str] = field(default_factory=list)


def extract_json(text: str) -> dict:
    """Pull the first JSON object out of a model reply (handles fences and chatter)."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in reply")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("unterminated JSON object in reply")


def _s(v, n: int) -> str:
    return str(v if v is not None else "")[:n]


def _clamp01(v, default: float = 0.5) -> float:
    try:
        return min(1.0, max(0.0, float(v)))
    except (TypeError, ValueError):
        return default


def _int(v, default: int = 0) -> int:
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return default


def clean_debater(d: dict) -> dict:
    """Drop unknown keys and coerce sloppy values so small models are not rejected on trivia."""
    pos = d.get("position")
    if isinstance(pos, str):
        pos = {"verdict": pos, "confidence": d.get("confidence", 0.5)}
    pos = pos if isinstance(pos, dict) else {}
    findings = []
    for f in (d.get("findings") or [])[:12]:
        if not isinstance(f, dict):
            continue
        ls = _int(f.get("line_start"))
        le = max(_int(f.get("line_end"), ls), ls)
        sev = str(f.get("severity", "medium")).lower()
        findings.append({
            "category": _s(f.get("category"), 64) or "other", "file": _s(f.get("file"), 256),
            "line_start": ls, "line_end": le,
            "severity": sev if sev in {"low", "medium", "high", "critical"} else "medium",
            "claim": _s(f.get("claim"), 600), "evidence": _s(f.get("evidence"), 1200),
            "fix": _s(f.get("fix"), 1200),
        })
    crit = d.get("critique")
    critique = None
    if isinstance(crit, dict):
        critique = {k: _s(crit.get(k), 500) for k in ("strongest", "biggest_blind_spot", "missed_by_all")}
    return {
        "position": {"verdict": str(pos.get("verdict", "unsure")).lower().strip(),
                     "confidence": _clamp01(pos.get("confidence"))},
        "findings": findings,
        "changed": bool(d.get("changed", False)),
        "changed_reason": _s(d.get("changed_reason"), 800),
        "text": _s(d.get("text"), 2000),
        "critique": critique,
    }


def clean_judge(d: dict) -> dict:
    rulings = []
    for r in d.get("rulings") or []:
        if isinstance(r, dict) and "cluster_id" in r:
            rulings.append({"cluster_id": _s(r["cluster_id"], 64), "accept": bool(r.get("accept")),
                            "reason": _s(r.get("reason"), 800)})
    return {"rulings": rulings, "reason": _s(d.get("reason"), 1200),
            "recommendation": _s(d.get("recommendation"), 600)}


def clean_framer(d: dict) -> dict:
    return {"restatement": _s(d.get("restatement"), 1200).strip()}


CLEANERS = {DebaterOutput: clean_debater, JudgeOutput: clean_judge, FramerOutput: clean_framer}


class Runner:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._sem = asyncio.Semaphore(max(1, settings.parallelism))

    async def call(
        self,
        provider: LLMProvider,
        messages: list[dict],
        model_cls: type[BaseModel],
        schema: dict | None,
    ) -> CallResult:
        """One logical call: retry transport failures, and ask once more if the JSON is invalid."""
        res = CallResult()
        msgs = list(messages)
        repaired = False
        async with self._sem:
            while True:
                comp = await self._complete(provider, msgs, schema, res)
                if comp is None:
                    return res
                res.prompt_tokens += comp.prompt_tokens
                res.completion_tokens += comp.completion_tokens
                res.raw = comp.text
                try:
                    obj = CLEANERS[model_cls](extract_json(comp.text))
                    res.output = model_cls.model_validate(obj)
                    return res
                except (ValueError, ValidationError) as e:
                    if repaired:
                        res.error = f"invalid JSON from model: {str(e).splitlines()[0][:160]}"
                        return res
                    repaired = True
                    res.notes.append("repair requested")
                    msgs = msgs + [
                        {"role": "assistant", "content": comp.text[:2000]},
                        {"role": "user", "content": "That reply was not valid. Reply again with ONLY "
                                                    "the JSON object described in the instructions."},
                    ]

    async def _complete(self, provider, msgs, schema, res: CallResult):
        s = self.settings
        for attempt in range(s.call_retries + 1):
            res.attempts += 1
            try:
                return await asyncio.wait_for(
                    provider.chat(msgs, json_schema=schema, max_tokens=s.max_output_tokens,
                                  temperature=s.temperature, timeout=s.call_timeout_s),
                    timeout=s.call_timeout_s + 5,
                )
            except TimeoutError:
                res.error = f"timed out after {s.call_timeout_s:.0f}s"
            except ProviderError as e:
                res.error = str(e)
                if not e.retryable:
                    return None
            if attempt < s.call_retries:
                await asyncio.sleep(min(0.4 * 2**attempt, 4.0))
        return None
