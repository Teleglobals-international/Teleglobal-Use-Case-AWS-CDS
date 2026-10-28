"""
Central configuration for the MF Investor Assistant backend.

All secrets/credentials are read from the workspace .env file (never hard-coded).
AWS resource identifiers default to safe placeholders so the app runs locally
even when the corresponding cloud resources have not been provisioned yet.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# The .env lives at the repo root (one level above /backend).
_REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_REPO_ROOT / ".env")


class Settings:
    """Runtime settings sourced from environment variables."""

    # --- AWS core ---------------------------------------------------------
    AWS_ACCESS_KEY_ID: str = os.getenv("AWS_ACCESS_KEY_ID", "")
    AWS_SECRET_ACCESS_KEY: str = os.getenv("AWS_SECRET_ACCESS_KEY", "")
    AWS_REGION: str = os.getenv("AWS_REGION", "ap-south-1")
    AWS_ACCOUNT_ID: str = os.getenv("AWS_ACCOUNT_ID", "")

    # --- LLM / Bedrock ----------------------------------------------------
    LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "bedrock")
    LLM_MODEL: str = os.getenv(
        "LLM_MODEL", "apac.anthropic.claude-3-5-sonnet-20241022-v2:0"
    )

    # --- Bedrock Knowledge Base (optional, falls back to local KB) --------
    KNOWLEDGE_BASE_ID: str = os.getenv("KNOWLEDGE_BASE_ID", "")

    # --- Amazon Cognito (optional; falls back to in-memory auth if unset) --
    COGNITO_USER_POOL_ID: str = os.getenv("COGNITO_USER_POOL_ID", "")
    COGNITO_CLIENT_ID: str = os.getenv("COGNITO_CLIENT_ID", "")

    @property
    def cognito_enabled(self) -> bool:
        return bool(self.COGNITO_USER_POOL_ID and self.COGNITO_CLIENT_ID)

    # --- Messaging channels (optional, falls back to local simulation) ----
    SES_FROM_EMAIL: str = os.getenv("SES_FROM_EMAIL", "support@teleglobals.com")
    SES_CONFIGURATION_SET: str = os.getenv("SES_CONFIGURATION_SET", "")
    WHATSAPP_PHONE_NUMBER_ID: str = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
    ADVISOR_ALERT_EMAIL: str = os.getenv("ADVISOR_ALERT_EMAIL", "support@teleglobals.com")

    # --- DynamoDB (optional) ---------------------------------------------
    HANDOFF_TABLE: str = os.getenv("HANDOFF_TABLE", "mf-advisor-handoff-queue")

    # --- Behaviour flags --------------------------------------------------
    # When true, external side-effects (WhatsApp/DynamoDB) are simulated locally
    # instead of calling AWS. Default is FALSE so email goes out live via
    # Amazon SES (sesv2) at runtime. Set SIMULATE_CHANNELS=true to force the
    # local simulation fallback (e.g. when developing without AWS access).
    SIMULATE_CHANNELS: bool = os.getenv("SIMULATE_CHANNELS", "false").lower() == "true"

    @property
    def has_aws_credentials(self) -> bool:
        return bool(self.AWS_ACCESS_KEY_ID and self.AWS_SECRET_ACCESS_KEY)


@lru_cache
def get_settings() -> Settings:
    return Settings()
