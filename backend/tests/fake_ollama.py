"""A tiny stand-in for the Ollama HTTP API, served over a real socket, to test the real adapters."""

import socket
import threading
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from quorum.providers.fake import FakeProvider

MODELS = {
    "llama3.2:3b": ("llama", "LLAMA 3.2 COMMUNITY LICENSE AGREEMENT\nLlama 3.2 Version Release Date"),
    "qwen2.5:3b": ("qwen2", "Apache License\nVersion 2.0, January 2004"),
    "gemma3:4b": ("gemma3", "Gemma Terms of Use\nLast modified: February 21, 2024"),
}


def make_app(calls: list) -> FastAPI:
    app = FastAPI()

    @app.get("/api/version")
    def version():
        return {"version": "0.9.9-test"}

    @app.get("/api/tags")
    def tags():
        return {"models": [{"name": n, "details": {"family": f}} for n, (f, _) in MODELS.items()]}

    @app.post("/api/show")
    async def show(req: Request):
        name = (await req.json())["model"]
        if name not in MODELS:
            return JSONResponse({"error": "not found"}, 404)
        fam, lic = MODELS[name]
        return {"license": lic, "details": {"family": fam}}

    @app.post("/api/chat")
    async def chat(req: Request):
        body = await req.json()
        calls.append(body)
        if body["model"] not in MODELS:
            return JSONResponse({"error": "model not found"}, 404)
        # behave like fake:sharp so the whole pipeline produces real findings
        comp = await FakeProvider("sharp").chat(body["messages"])
        return {"message": {"role": "assistant", "content": comp.text},
                "prompt_eval_count": comp.prompt_tokens, "eval_count": comp.completion_tokens}

    return app


class Server:
    def __init__(self):
        self.calls: list = []
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        self.server = uvicorn.Server(uvicorn.Config(make_app(self.calls), host="127.0.0.1",
                                                    port=self.port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self):
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("fake ollama did not start")

    def __exit__(self, *a):
        self.server.should_exit = True
        self.thread.join(5)


