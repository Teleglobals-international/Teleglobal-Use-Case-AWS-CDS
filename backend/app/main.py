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

from . import auth
from .config import get_settings
from .knowledge_base import search_fund_docs
from .memory import get_memory
from .router import NormalisedMessage, normalise_email, normalise_whatsapp, route
from . import tools as toolbox

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("mf_assistant.main")

app = FastAPI(
    title="MF Investor Assistant",
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
    user_id: str = Field(..., description="Email used at sign-up (or phone for demo accounts)")
    password: str | None = Field(None, description="Required when Cognito auth is enabled")


class RegisterRequest(BaseModel):
    name: str = Field(..., min_length=1)
    user_id: str = Field(..., description="Email to register (or phone for local demo)")
    password: str | None = Field(None, description="Required when Cognito auth is enabled")
    language: str = Field("English")
    risk_tolerance: str = Field("Moderate")
    investment_goals: list[str] = Field(default_factory=list)


class ConfirmRequest(BaseModel):
    user_id: str = Field(..., description="Email being confirmed")
    code: str = Field(..., description="Verification code emailed by Cognito")


class ForgotPasswordRequest(BaseModel):
    user_id: str = Field(..., description="Email to send a reset code to")


class ResetPasswordRequest(BaseModel):
    user_id: str = Field(..., description="Email being reset")
    code: str = Field(..., description="Reset code emailed by Cognito")
    password: str = Field(..., description="New password")


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
        "auth": "cognito" if s.cognito_enabled else "local",
    }


@app.get("/api/homepage")
def homepage(user_id: str = "guest") -> dict:
    """Personalised homepage: recommendations + trending + quick actions."""
    profile = get_memory().get_profile(user_id)
    trending = [
        "Best tax saving funds",
        "How to start a SIP",
        "Large Cap Fund performance",
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
        "greeting": f"Welcome back, {profile.get('name')}" if user_id != "guest" else "Welcome to the MF Assistant",
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
    """Authenticate via Cognito (email + password) with a local fallback."""
    uid = _normalise_user_id(req.user_id)
    if uid == "guest":
        raise HTTPException(status_code=400, detail="'guest' is reserved.")

    # Cognito path: used for email accounts when the pool is configured.
    if get_settings().cognito_enabled and "@" in uid:
        if not req.password:
            raise HTTPException(status_code=400, detail="Password is required.")
        try:
            return auth.login(uid, req.password)
        except auth.AuthError as exc:
            status = 401 if exc.code in {"NotAuthorizedException", "UserNotConfirmedException"} else 404
            raise HTTPException(status_code=status, detail=exc.message)

    # Local fallback (seed demo accounts / phone ids / Cognito disabled).
    mem = get_memory()
    if not mem.exists(uid):
        raise HTTPException(status_code=404, detail="No account found. Please register, or continue as Guest.")
    return {"status": "ok", "profile": mem.get_profile(uid)}


@app.post("/api/register")
def register(req: RegisterRequest) -> dict:
    """Register a user. With Cognito, emails a verification code (confirm step).
    Without Cognito, creates an in-memory profile and signs in immediately."""
    uid = _normalise_user_id(req.user_id)
    if uid == "guest":
        raise HTTPException(status_code=400, detail="'guest' is reserved.")

    profile = {
        "name": req.name.strip(),
        "channel": "email" if "@" in uid else "whatsapp",
        "language": req.language or "English",
        "risk_tolerance": req.risk_tolerance or "Moderate",
        "investment_goals": req.investment_goals or [],
        "kyc_status": "Pending",
        "holdings": [],
    }

    # Cognito path (email accounts): sign up -> code emailed -> confirm step.
    if get_settings().cognito_enabled and "@" in uid:
        if not req.password:
            raise HTTPException(status_code=400, detail="Password is required.")
        try:
            auth.register(uid, req.password, profile)
            return {"status": "confirmation_required", "user_id": uid}
        except auth.AuthError as exc:
            status = 409 if exc.code == "UsernameExistsException" else 400
            raise HTTPException(status_code=status, detail=exc.message)

    # Local fallback: create and sign in immediately.
    try:
        created = get_memory().register(uid, profile)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"status": "ok", "profile": created}


@app.post("/api/confirm")
def confirm(req: ConfirmRequest) -> dict:
    """Confirm a Cognito sign-up with the emailed code, then return the profile."""
    uid = _normalise_user_id(req.user_id)
    if not get_settings().cognito_enabled:
        raise HTTPException(status_code=400, detail="Confirmation is not required for this account.")
    try:
        auth.confirm(uid, req.code.strip())
        return {"status": "ok", "profile": auth.get_profile(uid)}
    except auth.AuthError as exc:
        raise HTTPException(status_code=400, detail=exc.message)


@app.post("/api/forgot-password")
def forgot_password(req: ForgotPasswordRequest) -> dict:
    """Start the Cognito password-reset flow (emails a reset code)."""
    uid = _normalise_user_id(req.user_id)
    if not get_settings().cognito_enabled:
        raise HTTPException(status_code=400, detail="Password reset requires Cognito auth.")
    try:
        return auth.forgot_password(uid)
    except auth.AuthError as exc:
        raise HTTPException(status_code=400, detail=exc.message)


@app.post("/api/reset-password")
def reset_password(req: ResetPasswordRequest) -> dict:
    """Complete the password reset with the emailed code and a new password."""
    uid = _normalise_user_id(req.user_id)
    if not get_settings().cognito_enabled:
        raise HTTPException(status_code=400, detail="Password reset requires Cognito auth.")
    try:
        return auth.reset_password(uid, req.code.strip(), req.password)
    except auth.AuthError as exc:
        raise HTTPException(status_code=400, detail=exc.message)


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
        subject="Re: your mutual fund query",
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
