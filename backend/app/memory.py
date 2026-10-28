"""
AgentCore Memory - per-user conversation context with cross-channel
continuity (a WhatsApp user and the same person on web/email share a profile
keyed by a normalised user id).

This in-process store keeps the demo self-contained. In production this maps
to AgentCore Memory / DynamoDB. The interface is intentionally small so it can
be swapped without touching the agent.

It also holds the (demo) investor directory: the synthetic investors loaded
from data/investors.json plus any profiles created at runtime via register().
Authentication here is deliberately lightweight (synthetic data, no real PII).
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
def _seed_profiles() -> dict[str, Any]:
    with open(_PROFILES, encoding="utf-8") as fh:
        return json.load(fh)["investors"]


class Memory:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._history: dict[str, deque] = defaultdict(lambda: deque(maxlen=_MAX_TURNS))
        # Runtime-registered investor profiles (keyed by normalised user id).
        self._registered: dict[str, dict[str, Any]] = {}

    # --- profiles ---------------------------------------------------------
    def _lookup(self, user_id: str) -> dict[str, Any] | None:
        if user_id in self._registered:
            return self._registered[user_id]
        return _seed_profiles().get(user_id)

    def get_profile(self, user_id: str) -> dict[str, Any]:
        # Cognito is the source of truth for registered users when enabled.
        from . import auth  # local import to avoid a cycle
        if auth.cognito_enabled() and user_id not in _seed_profiles() and "@" in user_id:
            try:
                if auth.user_exists(user_id):
                    return auth.get_profile(user_id)
            except Exception:  # noqa: BLE001 - fall back to local
                pass
        profile = self._lookup(user_id) or _seed_profiles()["guest"]
        return profile | {"user_id": user_id}

    def exists(self, user_id: str) -> bool:
        from . import auth  # local import to avoid a cycle
        if auth.cognito_enabled() and "@" in user_id:
            try:
                if auth.user_exists(user_id):
                    return True
            except Exception:  # noqa: BLE001
                pass
        return self._lookup(user_id) is not None

    def register(self, user_id: str, profile: dict[str, Any]) -> dict[str, Any]:
        """Create a new in-memory investor profile. Raises if it already exists."""
        with self._lock:
            if user_id in self._registered or user_id in _seed_profiles():
                raise ValueError("An account with this ID already exists.")
            self._registered[user_id] = profile
        return self.get_profile(user_id)

    def update_profile(self, user_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        """Patch an investor profile. For Cognito users, persist KYC status back
        to the user pool; otherwise update the in-memory/seed-copy store."""
        from . import auth  # local import to avoid a cycle
        if auth.cognito_enabled() and "@" in user_id and user_id not in _seed_profiles():
            try:
                if auth.user_exists(user_id):
                    if "kyc_status" in changes:
                        auth.set_kyc_status(user_id, changes["kyc_status"])
                    return auth.get_profile(user_id)
            except Exception:  # noqa: BLE001 - fall back to local
                pass
        with self._lock:
            if user_id not in self._registered:
                base = _seed_profiles().get(user_id)
                if base is None:
                    raise ValueError("No such account.")
                self._registered[user_id] = dict(base)
            self._registered[user_id].update(changes)
        return self.get_profile(user_id)

    # --- history ----------------------------------------------------------
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
