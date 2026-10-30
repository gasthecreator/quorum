"""Model registry (B2): config seeds, Ollama auto-discovery, OpenAI-compatible endpoints, metadata."""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
import yaml

from .config import Settings
from .providers import LLMProvider, ProviderError
from .providers.fake import FakeProvider
from .providers.ollama import OllamaProvider, list_ollama_models
from .providers.openai_compat import OpenAICompatProvider
from .schemas import ModelCreate, ModelInfo
from .store import Store

FAKE_TAGS = ["fake:sharp", "fake:sloppy", "fake:stubborn", "fake:judge-skeptic", "fake:broken"]

# Matched against the license text Ollama returns from /api/show.
LICENSE_PATTERNS: list[tuple[str, str]] = [
    (r"apache license", "Apache-2.0"),
    (r"\bmit license\b", "MIT"),
    (r"llama\s*3\.\d.*community license|llama 3 community license", "Llama Community License"),
    (r"llama.*community license", "Llama Community License"),
    (r"gemma terms of use|gemma.*prohibited use", "Gemma Terms of Use"),
    (r"qwen research license", "Qwen Research License"),
    (r"qwen license", "Qwen License"),
    (r"deepseek license", "DeepSeek License"),
    (r"creative commons", "Creative Commons"),
    (r"mistral research license", "Mistral Research License"),
]

# Fallback when the server gives no license text. Deliberately conservative.
FAMILY_HINTS: list[tuple[str, str]] = [
    (r"^(llama|codellama)", "llama"), (r"^qwen", "qwen"), (r"^gemma", "gemma"),
    (r"^phi", "phi"), (r"^mistral|^mixtral", "mistral"), (r"^deepseek", "deepseek"),
    (r"^granite", "granite"), (r"^smollm", "smollm"), (r"^olmo", "olmo"),
]


def detect_license(text: str) -> str | None:
    low = (text or "").lower()
    for pat, name in LICENSE_PATTERNS:
        if re.search(pat, low):
            return name
    return None


def infer_family(tag: str, reported: str | None = None) -> str:
    if reported:
        return re.sub(r"\d+(\.\d+)*$", "", reported.lower()) or reported.lower()
    base = tag.split("/")[-1].split(":")[0].lower()
    for pat, fam in FAMILY_HINTS:
        if re.search(pat, base):
            return fam
    return re.split(r"[-_.\d]", base)[0] or "unknown"


def model_id(provider: str, tag: str, base_url: str | None = None) -> str:
    if provider == "openai_compat" and base_url:
        return f"{provider}:{urlparse(base_url).netloc}/{tag}"
    return f"{provider}:{tag}"


class RegistryError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


@dataclass
class _Cached:
    when: float
    value: list[dict]


class Registry:
    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self._providers: dict[str, LLMProvider] = {}
        self._tags_cache: _Cached | None = None

    # ------------------------------------------------------------------ startup

    def load_config(self) -> None:
        """Seed from config/models.yaml and, when enabled, the simulated models."""
        path = self.settings.models_yaml
        if path.is_file():
            data = yaml.safe_load(path.read_text()) or {}
            for entry in data.get("models") or []:
                create = ModelCreate.model_validate({**entry, "calibrate": False})
                self.store.upsert_model(self._record(create, source="config"))
        if self.settings.enable_fake:
            for tag in FAKE_TAGS:
                rec = {
                    "id": model_id("fake", tag), "provider": "fake", "tag": tag, "base_url": None,
                    "api_key_env": None, "family": f"simulated-{tag.split(':')[1]}",
                    "license": "n/a (simulated, not a language model)", "open_weight": False,
                    "status": "registered", "status_detail": "Simulated model for offline testing",
                    "source": "fake",
                }
                if not self.store.get_model(rec["id"]):
                    self.store.upsert_model(rec)

    def default_judge(self) -> str | None:
        path = self.settings.models_yaml
        if path.is_file():
            return (yaml.safe_load(path.read_text()) or {}).get("judge")
        return None

    # ------------------------------------------------------------------ discovery

    async def ollama_tags(self, max_age: float = 5.0) -> list[dict] | None:
        """Installed Ollama models, or None if the server is unreachable."""
        now = time.monotonic()
        if self._tags_cache and now - self._tags_cache.when < max_age:
            return self._tags_cache.value
        try:
            tags = await list_ollama_models(self.settings.ollama_url)
        except ProviderError:
            self._tags_cache = None
            return None
        self._tags_cache = _Cached(now, tags)
        return tags

    async def _show(self, tag: str) -> dict:
        try:
            async with httpx.AsyncClient(base_url=self.settings.ollama_url) as c:
                r = await c.post("/api/show", json={"model": tag}, timeout=5.0)
                return r.json() if r.status_code == 200 else {}
        except (httpx.HTTPError, ValueError):
            return {}

    async def refresh(self) -> None:
        """Sync with Ollama: register installed tags, flag missing ones unavailable."""
        tags = await self.ollama_tags()
        if tags is None:
            for m in self.store.list_models():
                if m["provider"] == "ollama" and m["status"] != "calibrating":
                    self.store.set_model_status(m["id"], "unavailable", "Ollama is not reachable")
            return
        installed = {t["name"] for t in tags} | {t["name"].removesuffix(":latest") for t in tags}
        known = {m["id"]: m for m in self.store.list_models()}
        for t in tags:
            mid = model_id("ollama", t["name"])
            if mid in known:
                continue
            info = await self._show(t["name"])
            details = t.get("details") or info.get("details") or {}
            lic = detect_license(info.get("license", "")) or "unknown (see model card)"
            self.store.upsert_model({
                "id": mid, "provider": "ollama", "tag": t["name"], "base_url": None,
                "api_key_env": None, "family": infer_family(t["name"], details.get("family")),
                "license": lic, "open_weight": True, "status": "registered",
                "status_detail": "Discovered in Ollama", "source": "ollama",
            })
        for m in self.store.list_models():
            if m["provider"] != "ollama" or m["status"] == "calibrating":
                continue
            if m["tag"] not in installed:
                self.store.set_model_status(m["id"], "unavailable", "Not pulled in Ollama")
            elif m["status"] == "unavailable":
                self.store.set_model_status(m["id"], "registered", "")

    # ------------------------------------------------------------------ CRUD

    def _record(self, c: ModelCreate, *, source: str, info: dict | None = None) -> dict:
        info = info or {}
        lic = c.license or detect_license(info.get("license", "")) or "unknown (see model card)"
        open_weight = c.open_weight if c.open_weight is not None else (c.provider == "ollama")
        detail = ""
        if c.provider == "openai_compat" and c.open_weight is None:
            detail = "open_weight not asserted; pass open_weight=true if the weights are open"
        return {
            "id": model_id(c.provider, c.tag, c.base_url), "provider": c.provider, "tag": c.tag,
            "base_url": c.base_url, "api_key_env": c.api_key_env,
            "family": c.family or infer_family(c.tag, (info.get("details") or {}).get("family")),
            "license": lic, "open_weight": open_weight, "status": "registered",
            "status_detail": detail, "source": source,
        }

    async def add(self, c: ModelCreate) -> ModelInfo:
        if c.provider == "fake":
            if not self.settings.enable_fake:
                raise RegistryError("fake_disabled", "Simulated models are disabled.", 400)
            if c.tag not in FAKE_TAGS:
                raise RegistryError("unknown_fake", f"Unknown simulated tag. Use one of {FAKE_TAGS}.")
        if c.provider == "openai_compat":
            if not c.base_url or not c.base_url.startswith(("http://", "https://")):
                raise RegistryError("base_url_required", "openai_compat needs an http(s) base_url.")
            if c.api_key_env and not os.environ.get(c.api_key_env):
                raise RegistryError("api_key_missing",
                                    f"Environment variable {c.api_key_env} is not set.")
        info: dict = {}
        if c.provider == "ollama":
            tags = await self.ollama_tags(max_age=0)
            if tags is None:
                raise RegistryError("ollama_unreachable",
                                    f"Ollama is not reachable at {self.settings.ollama_url}.", 503)
            names = {t["name"] for t in tags}
            if c.tag not in names and f"{c.tag}:latest" not in names:
                raise RegistryError("model_not_installed",
                                    f"'{c.tag}' is not pulled. Run: ollama pull {c.tag}", 409)
            info = await self._show(c.tag)
        rec = self._record(c, source="api", info=info)
        existing = self.store.get_model(rec["id"])
        if existing:
            rec["status"] = existing["status"]
        self.store.upsert_model(rec)
        return self.info(rec["id"])

    def info(self, mid: str) -> ModelInfo:
        m = self.store.get_model(mid)
        if not m:
            raise RegistryError("unknown_model", f"Unknown model '{mid}'.", 404)
        installed = None
        if m["provider"] == "ollama" and self._tags_cache:
            names = {t["name"] for t in self._tags_cache.value}
            installed = m["tag"] in names or f"{m['tag']}:latest" in names
        return ModelInfo(**{k: m[k] for k in (
            "id", "provider", "tag", "base_url", "family", "license", "open_weight", "status",
            "status_detail")}, installed=installed)

    async def list(self) -> list[ModelInfo]:
        await self.refresh()
        return [self.info(m["id"]) for m in self.store.list_models()]

    def require(self, ids: list[str]) -> None:
        missing = [i for i in ids if not self.store.get_model(i)]
        if missing:
            raise RegistryError("unknown_model", f"Unknown model(s): {', '.join(missing)}", 404)

    # ------------------------------------------------------------------ providers

    def provider(self, mid: str) -> LLMProvider:
        if mid in self._providers:
            return self._providers[mid]
        m = self.store.get_model(mid)
        if not m:
            raise RegistryError("unknown_model", f"Unknown model '{mid}'.", 404)
        if m["provider"] == "ollama":
            p: LLMProvider = OllamaProvider(self.settings.ollama_url, m["tag"])
        elif m["provider"] == "openai_compat":
            key = os.environ.get(m["api_key_env"]) if m["api_key_env"] else None
            p = OpenAICompatProvider(m["base_url"], m["tag"], key)
        elif m["provider"] == "fake":
            p = FakeProvider(m["tag"])
        else:  # pragma: no cover
            raise RegistryError("bad_provider", f"Unsupported provider {m['provider']}")
        self._providers[mid] = p
        return p

    def set_provider(self, mid: str, provider: LLMProvider) -> None:
        """Inject a provider (tests)."""
        self._providers[mid] = provider

    async def aclose(self) -> None:
        for p in self._providers.values():
            await p.aclose()
        self._providers.clear()
