import json

import httpx
import pytest

from quorum.api import create_app

BUG = ('def get_user(db, name):\n    query = "SELECT * FROM users WHERE name = \'" + name + "\'"\n'
       '    cur = db.execute(query)\n    return cur.fetchone()\n')


@pytest.fixture
async def client(settings):
    app = create_app(settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        yield c
        for t in list(app.state.q.tasks):
            await t


def frames(text: str):
    out = []
    for block in text.split("\n\n"):
        ev = {}
        for line in block.splitlines():
            if ": " in line and not line.startswith(":"):
                k, v = line.split(": ", 1)
                ev[k] = v
        if "event" in ev:
            out.append((int(ev["id"]), ev["event"], json.loads(ev["data"])))
    return out


async def wait_stream(client, url):
    r = await client.get(url)
    assert r.headers["content-type"].startswith("text/event-stream")
    return frames(r.text)


async def model_ids(client):
    return {m["tag"]: m["id"] for m in (await client.get("/models")).json()}


async def test_health_and_packs(client):
    h = (await client.get("/health")).json()
    assert h["ok"] and h["contract_version"] and h["fake_models_enabled"]
    packs = (await client.get("/packs")).json()
    assert {p["id"] for p in packs} == {"code-review-dev", "claim-check-dev"}
    cr = next(p for p in packs if p["id"] == "code-review-dev")
    assert cr["corpus_counts"] == {"calibration": 9, "heldout": 8}


async def test_models_listed(client):
    models = (await client.get("/models")).json()
    assert len(models) == 5
    m = models[0]
    assert set(m) >= {"id", "provider", "tag", "family", "license", "open_weight", "status"}


async def test_full_run_over_sse(client):
    m = await model_ids(client)
    r = await client.post("/runs", json={"pack": "code-review-dev", "input": BUG, "seed": 1,
                                         "model_ids": [m["sharp"], m["sloppy"], m["stubborn"]]})
    assert r.status_code == 202
    run_id = r.json()["run_id"]
    evs = await wait_stream(client, f"/runs/{run_id}/events")
    names = [e for _, e, _ in evs]
    assert names[0] == "run_started" and names[-1] == "final"
    assert [s for s, _, _ in evs] == list(range(1, len(evs) + 1))
    # reconnect from the middle with Last-Event-ID
    tail = frames((await client.get(f"/runs/{run_id}/events", headers={"Last-Event-ID": "3"})).text)
    assert tail[0][0] == 4
    stored = (await client.get(f"/runs/{run_id}")).json()
    assert stored["status"] == "done" and stored["final"]["verdict"]["overall"] == "issue"
    assert len(stored["events"]) == len(evs)


async def test_run_validation_errors(client):
    m = await model_ids(client)
    r = await client.post("/runs", json={"pack": "nope", "input": "x", "model_ids": [m["sharp"]]})
    assert r.status_code == 404 and r.json()["code"] == "pack_error"
    r = await client.post("/runs", json={"pack": "code-review-dev", "input": "x",
                                         "model_ids": ["ollama:ghost"]})
    assert r.status_code == 404 and r.json()["code"] == "unknown_model"
    r = await client.post("/runs", json={"pack": "code-review-dev", "input": "", "model_ids": []})
    assert r.status_code == 422 and r.json()["code"] == "validation_error"
    assert (await client.get("/runs/zzz")).status_code == 404
    assert (await client.get("/runs/zzz/events")).json()["code"] == "unknown_run"


async def test_all_models_failing_ends_with_error_event(client):
    m = await model_ids(client)
    r = await client.post("/runs", json={"pack": "code-review-dev", "input": BUG,
                                         "model_ids": [m["broken"]]})
    evs = await wait_stream(client, f"/runs/{r.json()['run_id']}/events")
    assert evs[-1][1] == "error" and evs[-1][2]["code"] == "all_models_failed"


async def test_calibrate_job_and_profiles(client):
    m = await model_ids(client)
    r = await client.post("/calibrate", json={"model_ids": [m["sharp"]], "pack": "code-review-dev"})
    assert r.status_code == 202
    evs = await wait_stream(client, f"/jobs/{r.json()['job_id']}/events")
    assert evs[-1][1] == "job_done"
    assert any(e == "job_progress" for _, e, _ in evs)
    prof = (await client.get("/profiles")).json()
    cell = prof["profiles"][m["sharp"]]["code-review-dev"]["sql_injection"]
    assert cell["n"] == 3 and 0 < cell["weight"] <= 1
    assert (await client.get("/models")).json()
    status = {x["id"]: x["status"] for x in (await client.get("/models")).json()}
    assert status[m["sharp"]] == "ready"


async def test_add_model_starts_calibration(client):
    r = await client.post("/models", json={"provider": "fake", "tag": "sharp", "calibrate": True})
    assert r.status_code == 201 and r.json()["job_id"]
    evs = await wait_stream(client, f"/jobs/{r.json()['job_id']}/events")
    assert evs[-1][1] == "job_done"
    job = (await client.get(f"/jobs/{r.json()['job_id']}")).json()
    assert job["status"] == "done"


async def test_add_model_errors(client):
    r = await client.post("/models", json={"provider": "openai_compat", "tag": "x"})
    assert r.json()["code"] == "base_url_required"
    r = await client.post("/models", json={"provider": "openai_compat", "tag": "x",
                                           "base_url": "http://127.0.0.1:1/v1",
                                           "api_key_env": "QUORUM_DEFINITELY_UNSET"})
    assert r.json()["code"] == "api_key_missing"
    r = await client.post("/models", json={"provider": "ollama", "tag": "nothing:1b"})
    assert r.status_code in (503, 409)


async def test_add_openai_compat_model_metadata(client):
    r = await client.post("/models", json={
        "provider": "openai_compat", "tag": "llama-3.2-3b", "base_url": "http://127.0.0.1:1234/v1",
        "license": "Llama Community License", "open_weight": True, "calibrate": False})
    body = r.json()["model"]
    assert body["family"] == "llama" and body["open_weight"] is True
    assert body["id"] == "openai_compat:127.0.0.1:1234/llama-3.2-3b"


async def test_eval_job_and_latest(client):
    m = await model_ids(client)
    models = [m["sharp"], m["sloppy"], m["stubborn"]]
    j = (await client.post("/calibrate", json={"model_ids": models, "pack": "code-review-dev"})).json()
    await wait_stream(client, f"/jobs/{j['job_id']}/events")
    assert (await client.get("/eval/latest")).status_code == 404
    j = (await client.post("/eval", json={"pack": "code-review-dev", "model_ids": models,
                                          "judge_model_id": m["judge-skeptic"]})).json()
    evs = await wait_stream(client, f"/jobs/{j['job_id']}/events")
    assert evs[-1][1] == "job_done"
    latest = (await client.get("/eval/latest")).json()
    assert len(latest["conditions"]) == 6 and latest["n_items"] == 8


async def test_cors_only_for_frontend_origin(client):
    ok = await client.options("/models", headers={"Origin": "http://127.0.0.1:5173",
                                                  "Access-Control-Request-Method": "GET"})
    assert ok.headers.get("access-control-allow-origin") == "http://127.0.0.1:5173"
    bad = await client.options("/models", headers={"Origin": "http://evil.example",
                                                   "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in bad.headers
