"""
AgentCore Gateway - the agent's tools.

Each function here is exposed to the foundation model as a callable tool
(see agent.py for the tool specs). They map 1:1 to the architecture:

    search_fund_docs    -> Knowledge Base
    send_whatsapp       -> socialmessaging:SendWhatsAppMessage
    send_email          -> sesv2:SendEmail
    get_service_status  -> Service API (mock, synthetic data)
    handoff_to_advisor  -> DynamoDB queue + SES alert

Side-effecting tools call real boto3 clients, but degrade to a local
simulation (recorded in OUTBOX) when channels are not provisioned, so the
end-to-end flow always completes.
"""
from __future__ import annotations

import logging
import random
import uuid
from datetime import datetime, timezone
from typing import Any

from .aws_clients import dynamodb, sesv2, socialmessaging
from .config import get_settings
from .knowledge_base import search_fund_docs as _kb_search

logger = logging.getLogger("mf_assistant.tools")

# In-memory record of simulated outbound messages / hand-offs, surfaced in the
# API response so the UI (and tests) can show what "would have been sent".
OUTBOX: list[dict[str, Any]] = []


def _record(kind: str, payload: dict[str, Any], simulated: bool) -> dict[str, Any]:
    entry = {
        "id": str(uuid.uuid4()),
        "kind": kind,
        "simulated": simulated,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **payload,
    }
    OUTBOX.append(entry)
    return entry


# --------------------------------------------------------------------------- #
# Tool implementations
# --------------------------------------------------------------------------- #
def search_fund_docs(query: str, top_k: int = 3) -> dict[str, Any]:
    """Semantic retrieval over Axis MF factsheets, SIDs and FAQs."""
    results = _kb_search(query, top_k=top_k)
    return {"query": query, "results": results, "count": len(results)}


def send_whatsapp(to: str, message: str, pdf_url: str | None = None) -> dict[str, Any]:
    """Send a WhatsApp reply via AWS End User Messaging Social."""
    s = get_settings()
    payload = {"to": to, "message": message, "pdf_url": pdf_url}
    if not s.SIMULATE_CHANNELS and s.WHATSAPP_PHONE_NUMBER_ID:
        try:
            resp = socialmessaging().send_whatsapp_message(
                originationPhoneNumberId=s.WHATSAPP_PHONE_NUMBER_ID,
                metaApiVersion="v20.0",
                message=_whatsapp_text_body(to, message).encode("utf-8"),
            )
            return _record("whatsapp", {**payload, "message_id": resp.get("messageId")}, simulated=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("WhatsApp send failed (%s); simulating.", exc)
    return _record("whatsapp", payload, simulated=True)


def _whatsapp_text_body(to: str, message: str) -> str:
    import json

    return json.dumps(
        {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": message},
        }
    )


def send_email(to: str, subject: str, body: str, attachment_url: str | None = None) -> dict[str, Any]:
    """Send an email via Amazon SES (sesv2)."""
    s = get_settings()
    full_body = body
    if attachment_url:
        full_body += f"\n\nAttachment: {attachment_url}"
    payload = {"to": to, "subject": subject, "body": full_body, "attachment_url": attachment_url}
    if not s.SIMULATE_CHANNELS:
        try:
            resp = sesv2().send_email(
                FromEmailAddress=s.SES_FROM_EMAIL,
                Destination={"ToAddresses": [to]},
                Content={
                    "Simple": {
                        "Subject": {"Data": subject, "Charset": "UTF-8"},
                        "Body": {"Text": {"Data": full_body, "Charset": "UTF-8"}},
                    }
                },
                **({"ConfigurationSetName": s.SES_CONFIGURATION_SET} if s.SES_CONFIGURATION_SET else {}),
            )
            return _record("email", {**payload, "message_id": resp.get("MessageId")}, simulated=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("SES send failed (%s); simulating.", exc)
    return _record("email", payload, simulated=True)


def get_service_status(user_id: str, service: str) -> dict[str, Any]:
    """Mock Service API: SIP / redemption / KYC status (synthetic data)."""
    service = service.lower().strip()
    synthetic = {
        "kyc": {
            "status": random.choice(["Verified", "Pending", "Under review"]),
            "detail": "KYC verified via KRA using PAN + Aadhaar." ,
        },
        "sip": {
            "status": "Active",
            "detail": "Monthly SIP of Rs 5,000 in Axis Bluechip Fund, next debit on the 5th.",
        },
        "redemption": {
            "status": "Processed",
            "detail": "Last redemption of Rs 10,000 credited (T+2 settlement).",
        },
    }
    info = synthetic.get(service, {"status": "Unknown", "detail": "No record found for this service."})
    return {"user_id": user_id, "service": service, **info}


def handoff_to_advisor(user_id: str, reason: str, summary: str) -> dict[str, Any]:
    """Queue a human advisor hand-off (DynamoDB) and alert the advisor (SES)."""
    s = get_settings()
    ticket_id = f"HANDOFF-{uuid.uuid4().hex[:8].upper()}"
    item = {
        "ticket_id": ticket_id,
        "user_id": user_id,
        "reason": reason,
        "summary": summary,
        "status": "QUEUED",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    persisted = False
    if not s.SIMULATE_CHANNELS:
        try:
            dynamodb().Table(s.HANDOFF_TABLE).put_item(Item=item)
            persisted = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("DynamoDB put_item failed (%s); simulating.", exc)

    # Alert the advisor by email regardless of queue backend.
    send_email(
        to=s.ADVISOR_ALERT_EMAIL,
        subject=f"[Advisor hand-off] {ticket_id} - {reason}",
        body=f"A new investor hand-off has been queued.\n\nUser: {user_id}\nReason: {reason}\n\nSummary:\n{summary}",
    )
    return _record("handoff", {**item, "persisted": persisted}, simulated=not persisted)
