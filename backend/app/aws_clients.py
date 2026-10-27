"""
Lazily-created, cached boto3 clients.

Qualifying services used at runtime per the CDS architecture:
  - bedrock-runtime        -> AgentCore Runtime foundation model (Converse API)
  - bedrock-agent-runtime  -> Knowledge Bases retrieval
  - sesv2                  -> outbound email (send_email tool)
  - socialmessaging        -> outbound WhatsApp (send_whatsapp tool)
  - dynamodb               -> advisor hand-off queue + investor profiles

Clients are created with credentials from .env (via config.Settings).
If a client/service is unavailable the calling tool degrades gracefully.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

import boto3
from botocore.config import Config

from .config import get_settings

logger = logging.getLogger("mf_assistant.aws")

_BOTO_CONFIG = Config(retries={"max_attempts": 3, "mode": "standard"})


def _session() -> boto3.Session:
    s = get_settings()
    return boto3.Session(
        aws_access_key_id=s.AWS_ACCESS_KEY_ID or None,
        aws_secret_access_key=s.AWS_SECRET_ACCESS_KEY or None,
        region_name=s.AWS_REGION,
    )


@lru_cache
def bedrock_runtime() -> Any:
    return _session().client("bedrock-runtime", config=_BOTO_CONFIG)


@lru_cache
def bedrock_agent_runtime() -> Any:
    return _session().client("bedrock-agent-runtime", config=_BOTO_CONFIG)


@lru_cache
def sesv2() -> Any:
    return _session().client("sesv2", config=_BOTO_CONFIG)


@lru_cache
def socialmessaging() -> Any:
    # AWS End User Messaging Social (WhatsApp) client.
    return _session().client("socialmessaging", config=_BOTO_CONFIG)


@lru_cache
def dynamodb() -> Any:
    return _session().resource("dynamodb", config=_BOTO_CONFIG)
