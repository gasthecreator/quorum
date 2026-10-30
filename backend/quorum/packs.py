"""Load packs from disk. Packs are plain files; the format is part of the contract.

    <pack_dir>/<pack_id>/pack.json
    <pack_dir>/<pack_id>/<corpus.calibration>   one CorpusItem JSON object per line
    <pack_dir>/<pack_id>/<corpus.heldout>
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from .schemas import ClaimLabel, CorpusItem, Pack, ReviewLabel


class PackError(ValueError):
    pass


_ITEM = TypeAdapter(CorpusItem)


def input_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode()).hexdigest()[:16]


class PackStore:
    def __init__(self, dirs: tuple[Path, ...]):
        self.dirs = dirs

    def _pack_dirs(self) -> dict[str, Path]:
        found: dict[str, Path] = {}
        for base in self.dirs:
            if not base.is_dir():
                continue
            for child in sorted(base.iterdir()):
                if (child / "pack.json").is_file() and child.name not in found:
                    found[child.name] = child
        return found

    def list(self) -> list[Pack]:
        packs = []
        for pid in self._pack_dirs():
            try:
                packs.append(self.get(pid))
            except PackError:
                continue  # a broken pack must not hide the good ones
        return packs

    def get(self, pack_id: str) -> Pack:
        d = self._pack_dirs().get(pack_id)
        if d is None:
            raise PackError(f"unknown pack '{pack_id}'")
        try:
            pack = Pack.model_validate(json.loads((d / "pack.json").read_text()))
        except (OSError, ValueError, ValidationError) as e:
            raise PackError(f"pack '{pack_id}' has an invalid pack.json: {e}") from e
        if pack.id != pack_id:
            raise PackError(f"pack.json id '{pack.id}' does not match directory '{pack_id}'")
        counts = {}
        for split in ("calibration", "heldout"):
            try:
                counts[split] = len(self.corpus(pack_id, split))
            except PackError:
                counts[split] = 0
        return pack.model_copy(update={"corpus_counts": counts})

    def corpus(self, pack_id: str, split: str) -> list[CorpusItem]:
        d = self._pack_dirs().get(pack_id)
        if d is None:
            raise PackError(f"unknown pack '{pack_id}'")
        pack = Pack.model_validate(json.loads((d / "pack.json").read_text()))
        rel = pack.corpus.get(split)  # type: ignore[arg-type]
        if rel is None:
            raise PackError(f"pack '{pack_id}' has no '{split}' corpus")
        path = d / rel
        if not path.is_file():
            raise PackError(f"pack '{pack_id}': missing corpus file {rel}")
        items = []
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if not line.strip():
                continue
            try:
                items.append(_ITEM.validate_json(line))
            except ValidationError as e:
                raise PackError(f"{rel}:{i}: {e.errors()[0]['msg']}") from e
        return items

    def validate(self, pack_id: str) -> list[str]:
        """Return a list of problems; empty means the pack is usable."""
        problems: list[str] = []
        pack = self.get(pack_id)
        cal = self.corpus(pack_id, "calibration")
        held = self.corpus(pack_id, "heldout") if "heldout" in pack.corpus else []
        cats = set(pack.categories)
        mode = pack.modes[0]
        for split, items in (("calibration", cal), ("heldout", held)):
            ids = [x.id for x in items]
            if len(ids) != len(set(ids)):
                problems.append(f"{split}: duplicate ids")
            for x in items:
                if x.category not in cats:
                    problems.append(f"{split}/{x.id}: category '{x.category}' not in pack")
                want = ReviewLabel if mode == "review" else ClaimLabel
                if not isinstance(x.label, want):
                    problems.append(f"{split}/{x.id}: label type does not match mode '{mode}'")
                if isinstance(x.label, ClaimLabel) and not x.label.source_url.startswith("http"):
                    problems.append(f"{split}/{x.id}: source_url must be a URL")
                if isinstance(x.label, ReviewLabel) and x.label.has_issue and not x.label.lines:
                    problems.append(f"{split}/{x.id}: buggy item needs label.lines")
        cal_ids = {x.id for x in cal}
        cal_hashes = {input_hash(x.input) for x in cal}
        for x in held:
            if x.id in cal_ids or input_hash(x.input) in cal_hashes:
                problems.append(f"heldout/{x.id}: overlaps the calibration split")
        return problems
