"""
Guardrails (mirrors Bedrock Guardrails in the architecture).

Responsibilities:
  - PII masking on inbound text before it reaches the model/logs.
  - A system-prompt policy that forbids personalised investment advice.
  - A compliance disclaimer appended to every outbound answer.

These run locally so the policy is enforced even if a managed Bedrock
Guardrail is not attached. When a Bedrock guardrail IS configured, the two
layers are complementary (defence in depth).
"""
from __future__ import annotations

import re

DISCLAIMER = (
    "\n\n---\n*Mutual fund investments are subject to market risks. Read all "
    "scheme related documents carefully. This assistant provides information "
    "only, not personalised investment advice.*"
)

# Conservative PII patterns relevant to Indian MF investors.
_PATTERNS = [
    (re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b"), "[PAN_MASKED]"),          # PAN
    (re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b"), "[AADHAAR_MASKED]"),        # Aadhaar
    (re.compile(r"\b\d{9,18}\b"), "[ACCOUNT_MASKED]"),                     # bank acct
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[EMAIL_MASKED]"),
    (re.compile(r"\b(?:\+?91[-\s]?)?[6-9]\d{9}\b"), "[PHONE_MASKED]"),
]

ADVISORY_POLICY = (
    "You are an information assistant, NOT a financial advisor. You MUST NOT: "
    "recommend that a specific user buy/sell/switch a specific fund, promise or "
    "predict returns, or give personalised tax/legal advice. You MAY: explain "
    "how products and processes work, compare published fund attributes, and "
    "surface factsheets. When a user asks 'what should I invest in' or for a "
    "recommendation, provide general educational information and offer to connect "
    "them with a human advisor via the handoff_to_advisor tool."
)


def mask_pii(text: str) -> str:
    """Redact PII from free text before model/log exposure."""
    masked = text
    for pattern, replacement in _PATTERNS:
        masked = pattern.sub(replacement, masked)
    return masked


def apply_disclaimer(answer: str) -> str:
    if "subject to market risks" in answer:
        return answer
    return answer.rstrip() + DISCLAIMER
