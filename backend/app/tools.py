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


def record_agent_turn(
    user_id: str,
    channel: str,
    query: str,
    intent: str,
    tools_used: list[str],
) -> dict[str, Any]:
    """Log a completed agent turn so the UI activity panel can show it."""
    return _record(
        "agent_turn",
        {
            "user_id": user_id,
            "channel": channel,
            "query": query[:120],
            "intent": intent,
            "tools_used": [t for t in tools_used if t],
        },
        simulated=False,
    )


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
    """Semantic retrieval over fund factsheets, SIDs and FAQs."""
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
            message_id = resp.get("MessageId")
            logger.info("SES send OK to %s (MessageId=%s)", to, message_id)
            return _record(
                "email",
                {**payload, "from": s.SES_FROM_EMAIL, "message_id": message_id, "delivery": "sent"},
                simulated=False,
            )
        except Exception as exc:  # noqa: BLE001
            # In live mode, surface the real failure instead of silently
            # pretending it was simulated — the error is visible in the OUTBOX.
            logger.error("SES send FAILED to %s: %s", to, exc)
            return _record(
                "email",
                {**payload, "from": s.SES_FROM_EMAIL, "delivery": "failed", "error": str(exc)},
                simulated=False,
            )
    return _record("email", {**payload, "delivery": "simulated"}, simulated=True)


def get_service_status(user_id: str, service: str) -> dict[str, Any]:
    """Mock Service API: SIP / redemption / KYC status (synthetic data).

    KYC status reflects the user's real (in-memory) profile so that completing
    KYC is consistent across turns. SIP/redemption remain synthetic samples.

    PRODUCTION NOTE (confidential investor data):
      In production this must NOT use synthetic data. Confidential details are
      sourced from authoritative systems only:
        - KYC / identity (PAN, Aadhaar, address proof): fetched from a
          consent-based identity/KYC provider (e.g. a government-backed document
          wallet or KRA) via its e-KYC / issued-documents APIs.
        - SIP / redemption / folio: the fund house transaction APIs.
      No PII is stored locally; the assistant only receives the user-consented
      verification result. See docs/ARCHITECTURE.md ("Confidential data sources").
    """
    from .memory import get_memory  # local import to avoid a cycle

    service = service.lower().strip()
    if service == "kyc":
        profile = get_memory().get_profile(user_id)
        status = profile.get("kyc_status", "Unknown")
        detail = {
            "Verified": "Your KYC is verified. You're all set to invest.",
            "Pending": "Your KYC is pending. Raise a request and our team will complete it.",
            "In progress": "Your KYC request is in progress; our verification team will reach out shortly.",
            "Under review": "Your KYC is under review; this usually takes 1-2 working days.",
        }.get(status, "KYC status is not available for your account.")
        return {"user_id": user_id, "service": "kyc", "status": status, "detail": detail}

    synthetic = {
        "sip": {
            "status": "Active",
            "detail": "Monthly SIP of Rs 5,000 in the Large Cap Fund, next debit on the 5th.",
        },
        "redemption": {
            "status": "Processed",
            "detail": "Last redemption of Rs 10,000 credited (T+2 settlement).",
        },
    }
    info = synthetic.get(service, {"status": "Unknown", "detail": "No record found for this service."})
    return {"user_id": user_id, "service": service, **info}


def request_kyc(user_id: str) -> dict[str, Any]:
    """Raise a KYC request for the logged-in investor into the processing queue.

    KYC cannot be self-verified instantly: it requires document verification by
    the identity/KYC team. So this records a request, marks the profile
    'In progress', alerts the ops team, and tells the investor the team will
    reach out. Status becomes 'Verified' only after the team processes it.

    PRODUCTION NOTE (confidential investor data):
      In production this request routes to a consent-driven e-KYC flow with an
      identity/KYC provider (PAN + Aadhaar via a document wallet / KRA). The
      assistant never collects or stores raw PAN/Aadhaar numbers; it only
      receives the final verification outcome from the provider.
    """
    from .memory import get_memory  # local import to avoid a cycle

    mem = get_memory()
    if user_id == "guest" or not mem.exists(user_id):
        return {
            "user_id": user_id,
            "status": "login_required",
            "detail": "Please log in or register before requesting KYC.",
        }

    current = mem.get_profile(user_id).get("kyc_status")
    if current == "Verified":
        return {"user_id": user_id, "status": "Verified", "already": True,
                "detail": "Your KYC is already verified."}
    if current == "In progress":
        return {"user_id": user_id, "status": "In progress", "already": True,
                "detail": "Your KYC request is already in progress; our team will reach out shortly."}

    ref = f"KYC-{uuid.uuid4().hex[:8].upper()}"
    mem.update_profile(user_id, {"kyc_status": "In progress"})

    # Alert the KYC/ops team (real email via SES in live mode).
    s = get_settings()
    send_email(
        to=s.ADVISOR_ALERT_EMAIL,
        subject=f"[KYC request] {ref} - {user_id}",
        body=(
            f"A new KYC verification request has been queued.\n\n"
            f"Investor: {user_id}\nReference: {ref}\nStatus: In progress\n\n"
            f"Please initiate the e-KYC / document verification for this investor."
        ),
    )
    return _record(
        "kyc",
        {
            "user_id": user_id,
            "status": "In progress",
            "reference": ref,
            "detail": "KYC request queued. Our verification team will reach out to complete your e-KYC.",
        },
        simulated=True,
    )


# Backwards-compatible alias (the agent used to call complete_kyc).
complete_kyc = request_kyc


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
