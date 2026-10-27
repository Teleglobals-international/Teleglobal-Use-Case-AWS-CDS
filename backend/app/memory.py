"""
AgentCore Memory - per-user conversation context with cross-channel
continuity (a WhatsApp user and the same person on web/email share a profile
keyed by a normalised user id).

This in-process store keeps the demo self-contained. In production this maps
to AgentCore Memory / DynamoDB. The interface is intentionally small so it can
be swapped without touching the agent.
"""
from __future__ import annotations

import json
import threading
from collections import defaultdict, deque
from functools import lru_cache
from pathlib import Path
from typing import Any

_PROFILES = Path(__file__).resolve().parent / "data" / "investors.json"
_MAX_TURNS = 12  # rolling window of recent messages retained per user


@lru_cache
def _profiles() -> dict[str, Any]:
    with open(_PROFILES, encoding="utf-8") as fh:
        return json.load(fh)["investors"]


class Memory:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._history: dict[str, deque] = defaultdict(lambda: deque(maxlen=_MAX_TURNS))

    def get_profile(self, user_id: str) -> dict[str, Any]:
        profiles = _profiles()
        return profiles.get(user_id, profiles["guest"]) | {"user_id": user_id}

    def get_history(self, user_id: str) -> list[dict[str, str]]:
        with self._lock:
            return list(self._history[user_id])

    def append(self, user_id: str, role: str, content: str) -> None:
        with self._lock:
            self._history[user_id].append({"role": role, "content": content})

    def reset(self, user_id: str) -> None:
        with self._lock:
            self._history.pop(user_id, None)


@lru_cache
def get_memory() -> Memory:
    return Memory()
