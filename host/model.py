"""
Model backends. The host streams output tokens from one of these.

Select with env: MODEL=mock (default) | ollama
MockModel needs no GPU. OllamaModel proxies a local Ollama server (Phase 1 on real hardware).
"""
from __future__ import annotations

import os
from typing import Iterator


class ModelBackend:
    name: str
    modality: str = "text"  # "text" | "image" | "code"; image models are gated by moderation

    def stream(self, prompt: str) -> Iterator[str]:
        """Yield output tokens one at a time."""
        raise NotImplementedError


class MockModel(ModelBackend):
    name = "mock-echo:1b"
    modality = "text"

    def stream(self, prompt: str) -> Iterator[str]:
        reply = (
            "This is a mock host streaming tokens. Your prompt was: "
            f"'{prompt[:80]}'. In Phase 1 this is a real open model via vLLM or Ollama."
        )
        for tok in reply.split():
            yield tok + " "


class OllamaModel(ModelBackend):
    """Phase 1: stream from a local Ollama server (http://127.0.0.1:11434)."""

    def __init__(self, name: str) -> None:
        self.name = name

    def stream(self, prompt: str) -> Iterator[str]:
        import json
        import httpx

        url = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434") + "/api/generate"
        # keep_alive pins the model in VRAM so it doesn't cold-load per request (cold loads are
        # the main cause of the first chunk blowing past timeouts). Generous read timeout covers
        # a cold first token if it does happen.
        payload = {"model": self.name, "prompt": prompt,
                   "keep_alive": os.getenv("OLLAMA_KEEP_ALIVE", "30m")}
        timeout = httpx.Timeout(connect=10.0, read=300.0, write=10.0, pool=10.0)
        with httpx.stream("POST", url, json=payload, timeout=timeout) as r:
            for line in r.iter_lines():
                if not line:
                    continue
                chunk = json.loads(line)
                if chunk.get("response"):
                    yield chunk["response"]


def get_backend() -> ModelBackend:
    kind = os.getenv("MODEL", "mock").lower()
    if kind == "mock":
        return MockModel()
    if kind == "ollama":
        return OllamaModel(os.getenv("OLLAMA_MODEL", "llama3.1"))
    raise ValueError(f"unknown MODEL backend: {kind}")
