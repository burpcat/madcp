# MadCP — Proprietary. Copyright (c) 2026 AVINASH ARUTLAights Reserved. See LICENSE.
# madhu/workers/providers/__init__.py
from __future__ import annotations

"""
Provider registry for MadCP workers.

To add a new provider:
1. Create madhu/workers/providers/{name}.py implementing the Provider protocol
2. Import the class here and add it to PROVIDER_REGISTRY
3. Set provider: "{name}" in the relevant tier YAML config

No plugin auto-discovery. Explicit registration only.
"""

from madhu.workers.providers.ollama import OllamaProvider

PROVIDER_REGISTRY: dict[str, type] = {
    "ollama": OllamaProvider,
}