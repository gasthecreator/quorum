import httpx
import pytest

from quorum.api import create_app
from quorum.providers import ProviderError
from quorum.providers.ollama import OllamaProvider
from quorum.providers.openai_compat import OpenAICompatProvider
from quorum.registry import detect_license, infer_family

from .fake_ollama import Server

BUG = ('def get_user(db, name):\n    query = "SELECT * FROM users WHERE name = \'" + name + "\'"\n'
       '    cur = db.execute(query)\n    return cur.fetchone()\n')


@pytest.fixture(scope="module")
def ollama():
    with Server() as s:
        yield s


def test_license_and_family_detection():
    assert detect_license("Apache License\nVersion 2.0") == "Apache-2.0"
    assert detect_license("MIT License\n\nCopyright") == "MIT"
    assert detect_license("LLAMA 3.2 COMMUNITY LICENSE AGREEMENT") == "Llama Community License"
    assert detect_license("Gemma Terms of Use") == "Gemma Terms of Use"
    assert detect_license("something else entirely") is None
    assert infer_family("qwen2.5:3b", "qwen2") == "qwen"
    assert infer_family("gemma3:4b", "gemma3") == "gemma"
    assert infer_family("phi4-mini:latest") == "phi"
    assert infer_family("hf.co/org/Mystery-7B:Q4") == "mystery"


async def test_ollama_provider_roundtrip_and_schema(ollama):
    p = OllamaProvider(ollama.url, "qwen2.5:3b")
    c = await p.chat([{"role": "system", "content": "You are one member of a panel of independent "
                                                     "code reviewers."},
                      {"role": "user", "content": "x"}], json_schema={"type": "object"})
    assert c.completion_tokens > 0
    assert ollama.calls[-1]["format"] == {"type": "object"}
    assert ollama.calls[-1]["options"]["num_predict"] == 700
    assert ollama.calls[-1]["stream"] is False
    await p.aclose()


async def test_ollama_missing_model_is_not_retryable(ollama):
    p = OllamaProvider(ollama.url, "ghost:1b")
    with pytest.raises(ProviderError) as e:
        await p.chat([{"role": "user", "content": "x"}])
    assert not e.value.retryable and "ollama pull" in str(e.value)


async def test_ollama_unreachable():
    p = OllamaProvider("http://127.0.0.1:1", "x")
    with pytest.raises(ProviderError):
        await p.chat([{"role": "user", "content": "x"}], timeout=1)


async def test_openai_compat_parsing_and_auth():
    seen = {}

    def handler(req: httpx.Request):
        seen["auth"] = req.headers.get("authorization")
        seen["body"] = req.content
        if req.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": {"content": "{\"a\": 1}"}}],
                                             "usage": {"prompt_tokens": 5, "completion_tokens": 3}})
        return httpx.Response(404)

    p = OpenAICompatProvider("http://x/v1", "m", "sekret")
    p._client = httpx.AsyncClient(base_url="http://x/v1", transport=httpx.MockTransport(handler),
                                  headers={"Authorization": "Bearer sekret"})
    c = await p.chat([{"role": "user", "content": "hi"}], json_schema={"type": "object"})
    assert c.text == '{"a": 1}' and c.prompt_tokens == 5
    assert seen["auth"] == "Bearer sekret" and b"json_object" in seen["body"]

    p._client = httpx.AsyncClient(base_url="http://x/v1", transport=httpx.MockTransport(
        lambda r: httpx.Response(401)))
    with pytest.raises(ProviderError) as e:
        await p.chat([{"role": "user", "content": "hi"}])
    assert not e.value.retryable


@pytest.fixture
async def live_client(settings, ollama):
    settings.ollama_url = ollama.url
    settings.enable_fake = False
    app = create_app(settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        yield c
        for t in list(app.state.q.tasks):
            await t


async def test_autodiscovery_lists_installed_models_with_licenses(live_client):
    h = (await live_client.get("/health")).json()
    assert h["ollama"]["reachable"] and h["ollama"]["version"] == "0.9.9-test"
    models = {m["tag"]: m for m in (await live_client.get("/models")).json()}
    assert set(models) == {"llama3.2:3b", "qwen2.5:3b", "gemma3:4b"}
    assert models["qwen2.5:3b"]["license"] == "Apache-2.0"
    assert models["llama3.2:3b"]["license"] == "Llama Community License"
    assert models["gemma3:4b"]["family"] == "gemma" and models["gemma3:4b"]["open_weight"] is True
    assert models["qwen2.5:3b"]["installed"] is True


async def test_add_ollama_model_checks_it_is_pulled_then_calibrates(live_client):
    r = await live_client.post("/models", json={"provider": "ollama", "tag": "mistral:7b"})
    assert r.status_code == 409 and r.json()["code"] == "model_not_installed"
    r = await live_client.post("/models", json={"provider": "ollama", "tag": "qwen2.5:3b",
                                                "packs": ["code-review-dev"]})
    assert r.status_code == 201
    job = r.json()["job_id"]
    body = (await live_client.get(f"/jobs/{job}/events")).text
    assert "job_done" in body
    prof = (await live_client.get("/profiles")).json()["profiles"]
    assert "ollama:qwen2.5:3b" in prof
    status = {m["tag"]: m["status"] for m in (await live_client.get("/models")).json()}
    assert status["qwen2.5:3b"] == "ready"


async def test_debate_through_real_ollama_adapter(live_client):
    ids = [m["id"] for m in (await live_client.get("/models")).json()]
    r = await live_client.post("/runs", json={"pack": "code-review-dev", "input": BUG,
                                              "model_ids": ids})
    body = (await live_client.get(f"/runs/{r.json()['run_id']}/events")).text
    assert "event: final" in body and '"overall": "issue"' in body
