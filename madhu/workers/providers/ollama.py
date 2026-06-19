# madhu/workers/providers/ollama.py
from __future__ import annotations

"""
Ollama provider for MadCP.

Implements the Provider protocol against the Ollama HTTP API.
Endpoint is configurable at construction time — defaults to localhost.

No worker logic here: no ticket awareness, no AST validation, no
channel-marker stripping. Returns raw model output only.
"""

import httpx

from madhu.workers.base import ProviderError


class OllamaProvider:
    """
    Provider implementation for Ollama.

    Calls the Ollama /api/generate endpoint synchronously (httpx sync client).
    Workers run in child processes with no event loop — sync is correct here.

    Config keys (passed as provider_config in hamsa.yaml, stage 10):
        endpoint: base URL, default "http://localhost:11434"

    generate() args (model, temperature, timeout) come from the tier config,
    not from this class — the worker passes them through.
    """

    def __init__(self, endpoint: str = "http://localhost:11434") -> None:
        """
        Initialise with an Ollama endpoint base URL.

        The /api/generate path is appended internally.
        """
        self.endpoint = endpoint.rstrip("/")
        self._generate_url = f"{self.endpoint}/api/generate"

    def generate(
        self,
        prompt: str,
        model: str,
        temperature: float,
        timeout: float,
    ) -> str:
        """
        Send prompt to Ollama and return the raw response string.

        Raises ProviderError on:
        - HTTP error (non-2xx)
        - Timeout
        - Connection error
        - Empty response body

        Returns raw text — no cleaning, no validation.
        """
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": temperature,
            },
        }

        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.post(self._generate_url, json=payload)
                response.raise_for_status()
        except httpx.TimeoutException:
            raise ProviderError(
                f"Ollama request timed out after {timeout}s"
            )
        except httpx.HTTPStatusError as exc:
            raise ProviderError(
                f"Ollama HTTP error: {exc.response.status_code}: "
                f"{exc.response.text[:200]}"
            )
        except httpx.RequestError as exc:
            raise ProviderError(
                f"Ollama connection error: {exc}"
            )

        data = response.json()
        text = data.get("response", "").strip()

        if not text:
            raise ProviderError("Ollama returned empty response")

        return text