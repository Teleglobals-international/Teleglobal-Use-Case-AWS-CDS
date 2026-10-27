"""
Lambda Router equivalent.

Normalises an inbound message from any channel (WhatsApp via SNS, Email via
SES/S3, or the website) into a common shape, then invokes the AgentCore agent.
Keeps channel-specific parsing out of the agent itself.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .agent import run_agent

logger = logging.getLogger("mf_assistant.router")


@dataclass
class NormalisedMessage:
    user_id: str
    text: str
    channel: str  # whatsapp | email | web
    page: str | None = None


def normalise_whatsapp(event: dict[str, Any]) -> NormalisedMessage:
    """Parse an AWS End User Messaging Social inbound event (via SNS)."""
    # Shape mirrors the WhatsApp Business inbound payload.
    msg = event.get("messages", [{}])[0] if event.get("messages") else {}
    user_id = msg.get("from") or event.get("from") or "unknown"
    text = (msg.get("text", {}) or {}).get("body") or event.get("text", "")
    return NormalisedMessage(user_id=f"+{user_id.lstrip('+')}", text=text, channel="whatsapp")


def normalise_email(event: dict[str, Any]) -> NormalisedMessage:
    """Parse a raw inbound email (SES receipt rule -> S3 MIME) summary."""
    user_id = event.get("from", "unknown")
    text = event.get("body") or event.get("subject", "")
    return NormalisedMessage(user_id=user_id, text=text, channel="email")


def route(message: NormalisedMessage) -> dict[str, Any]:
    logger.info("Routing %s message from %s", message.channel, message.user_id)
    return run_agent(
        user_id=message.user_id,
        message=message.text,
        channel=message.channel,
        page=message.page,
    )
