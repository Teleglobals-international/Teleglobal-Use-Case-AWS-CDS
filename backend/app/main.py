"""
FastAPI application - the public surface of the MF Investor Assistant.

Endpoints:
  GET  /health                 liveness + config summary
  GET  /api/homepage           personalised homepage (recommendations, trending)
  POST /api/chat               website/app chat + search (JSON)
  POST /webhooks/whatsapp      inbound WhatsApp event (End User Messaging -> SNS)
  POST /webhooks/email         inbound email event (SES receipt rule -> S3)
  GET  /api/outbox             simulated outbound messages / hand-offs (demo aid)
  POST /api/reset              clear a user's conversation memory
"""
from __future__ import annotations

import logging

import re

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .config import get_settings
from .knowledge_base import search_fund_docs
from .memory import get_memory
from .router import NormalisedMessage, normalise_email, normalise_whatsapp, route
from . import tools as toolbox

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("mf_assistant.main")

app = FastAPI(
    title="Axis AMC - MF Investor Assistant",
    version="0.1.0",
    description="Agentic WhatsApp + Email + Web self-service assistant (AWS Bedrock AgentCore reference implementation).",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class ChatRequest(BaseModel):
    message: str = Field(..., description="The user's query")
    user_id: str = Field("guest", description="Phone/email/web id for continuity")
    channel: str = Field("web", description="web | whatsapp | email")
    page: str | None = Field(None, description="Current page/context, e.g. 'KYC'")


class WhatsAppEvent(BaseModel):
    from_: str | None = Field(None, alias="from")
    text: str | None = None
    messages: list | None = None

    class Config:
        populate_by_name = True


class EmailEvent(BaseModel):
    from_: str = Field(..., alias="from")
    subject: str | None = ""
    body: str | None = ""

    class Config:
        populate_by_name = True


class LoginRequest(BaseModel):
    user_id: str = Field(..., description="Phone (+91...) or email used at sign-up")


class RegisterRequest(BaseModel):
    name: str = Field(..., min_length=1)
    user_id: str = Field(..., description="Phone (+91...) or email to register")
    language: str = Field("English")
    risk_tolerance: str = Field("Moderate")
    investment_goals: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/health")
def health() -> dict:
    s = get_settings()
    return {
        "status": "ok",
        "region": s.AWS_REGION,
        "model": s.LLM_MODEL,
        "aws_credentials": s.has_aws_credentials,
        "knowledge_base": "bedrock" if s.KNOWLEDGE_BASE_ID else "local",
        "channels": "simulated" if s.SIMULATE_CHANNELS else "live",
    }


@app.get("/api/homepage")
def homepage(user_id: str = "guest") -> dict:
    """Personalised homepage: recommendations + trending + quick actions."""
    profile = get_memory().get_profile(user_id)
    trending = [
        "Best tax saving funds",
        "How to start a SIP",
        "Axis Bluechip Fund performance",
        "Check my KYC status",
    ]
    # Personalise suggestions from the investor's goals/holdings (info only).
    suggestions = []
    for goal in profile.get("investment_goals", []):
        hits = search_fund_docs(goal, top_k=1)
        if hits:
            suggestions.append(
                {
                    "goal": goal,
                    "fund": hits[0]["fund"],
                    "pdf_url": hits[0]["pdf_url"],
                    "fund_url": hits[0].get("fund_url", hits[0]["pdf_url"]),
                }
            )
    return {
        "greeting": f"Welcome back, {profile.get('name')}" if user_id != "guest" else "Welcome to Axis MF",
        "profile": profile,
        "trending": trending,
        "personalised": suggestions,
    }


def _normalise_user_id(raw: str) -> str:
    """Normalise a login/register id: emails lowercased, phones to +91 form."""
    uid = (raw or "").strip()
    if "@" in uid:
        return uid.lower()
    digits = re.sub(r"\D", "", uid)
    if digits:
        # Keep the last 10 digits and prefix +91 (demo assumes Indian numbers).
        return "+91" + digits[-10:]
    return uid


@app.post("/api/login")
def login(req: LoginRequest) -> dict:
    """Authenticate against the synthetic/registered investor directory."""
    uid = _normalise_user_id(req.user_id)
    mem = get_memory()
    if uid == "guest" or not mem.exists(uid):
        raise HTTPException(
            status_code=404,
            detail="No account found for that ID. Please register, or continue as Guest.",
        )
    return {"status": "ok", "profile": mem.get_profile(uid)}


@app.post("/api/register")
def register(req: RegisterRequest) -> dict:
    """Create a new in-memory investor profile and 'log in' as that user."""
    uid = _normalise_user_id(req.user_id)
    if uid == "guest":
        raise HTTPException(status_code=400, detail="'guest' is reserved.")
    channel = "email" if "@" in uid else "whatsapp"
    profile = {
        "name": req.name.strip(),
        "channel": channel,
        "language": req.language or "English",
        "risk_tolerance": req.risk_tolerance or "Moderate",
        "investment_goals": req.investment_goals or [],
        "kyc_status": "Pending",
        "holdings": [],
    }
    try:
        created = get_memory().register(uid, profile)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"status": "ok", "profile": created}


@app.post("/api/chat")
def chat(req: ChatRequest) -> dict:
    result = route(
        NormalisedMessage(user_id=req.user_id, text=req.message, channel=req.channel, page=req.page)
    )
    # Log the web turn so the "Agent activity" panel reflects web chats too
    # (WhatsApp/email/hand-off tools already record themselves).
    toolbox.record_agent_turn(
        user_id=req.user_id,
        channel=req.channel,
        query=req.message,
        intent=result.get("intent", "information"),
        tools_used=[t.get("tool") for t in result.get("tool_trace", [])],
    )
    return result


@app.post("/webhooks/whatsapp")
def whatsapp_webhook(event: WhatsAppEvent) -> dict:
    msg = normalise_whatsapp(event.model_dump(by_alias=True))
    result = route(msg)
    # Mirror the architecture: agent replies on WhatsApp via socialmessaging.
    toolbox.send_whatsapp(
        to=msg.user_id,
        message=result["answer"],
        pdf_url=_first_pdf(result),
    )
    return result


@app.post("/webhooks/email")
def email_webhook(event: EmailEvent) -> dict:
    msg = normalise_email(event.model_dump(by_alias=True))
    result = route(msg)
    toolbox.send_email(
        to=msg.user_id,
        subject="Re: your Axis MF query",
        body=result["answer"],
        attachment_url=_first_pdf(result),
    )
    return result


@app.get("/api/outbox")
def outbox() -> dict:
    return {"messages": toolbox.OUTBOX[-50:], "count": len(toolbox.OUTBOX)}


@app.post("/api/reset")
def reset(user_id: str = "guest") -> dict:
    get_memory().reset(user_id)
    return {"status": "reset", "user_id": user_id}


def _first_pdf(result: dict) -> str | None:
    for cta in result.get("ctas", []):
        if cta.get("url", "").endswith(".pdf"):
            return cta["url"]
    return None
