"""
AgentCore Runtime - the MF Assistant agent.

Implements the agentic loop over Amazon Bedrock using the Converse API with
tool use (function calling). The agent:

  1. loads per-user context from Memory and applies Guardrails (policy + PII),
  2. retrieves fund knowledge and invokes gateway tools as needed,
  3. produces a channel-appropriate answer with intent-based CTAs,
  4. persists the turn back to Memory for cross-channel continuity.

If Bedrock is unreachable, a deterministic fallback still answers from the
local knowledge base so the demo never dead-ends.
"""
from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from typing import Any

from .aws_clients import bedrock_runtime
from .config import get_settings
from .guardrails import ADVISORY_POLICY, apply_disclaimer, mask_pii
from .knowledge_base import search_fund_docs
from .memory import get_memory
from . import tools as toolbox

logger = logging.getLogger("mf_assistant.agent")

MAX_TOOL_ITERATIONS = 5

SYSTEM_PROMPT = f"""You are the Axis Mutual Fund Investor Assistant, a helpful,
accurate, multilingual (English + Hinglish) assistant for mutual fund investors
across WhatsApp, Email and the Axis MF website/app.

{ADVISORY_POLICY}

Guidelines:
- Understand the user's INTENT (information, transaction, service, advice) and
  CONTEXT (their profile, risk tolerance, goals, current page).
- Ground every factual claim in the knowledge base. Use the search_fund_docs
  tool before answering fund/service questions. Never invent fund figures.
- Always propose relevant INTENT-BASED CTAs (call-to-action) as a JSON list in
  your final answer's "ctas" field, e.g. "View fund page", "Start SIP",
  "Download SID / factsheet", "Check KYC status", "Talk to an advisor".
- CRITICAL on URLs: NEVER invent or guess a URL. For a CTA "url", use ONLY the
  exact "fund_url" or "pdf_url" values returned by the search_fund_docs tool for
  the fund you are discussing. If you do not have a real URL from the tool for a
  CTA, set its "url" to "" (empty string) and the app will handle it. Do not
  construct links like https://www.axismf.com/talk-to-advisor.
- For the advisor CTA, always use an empty url ("") so it opens the in-app
  hand-off, never a web page.
- LOGIN: This app HAS a built-in login/register. If a Guest user asks about
  account-specific things (KYC status, portfolio, SIP status), briefly explain
  they need to sign in, then offer CTAs with action "login" (label "Login") and
  action "register" (label "Register") and empty url. Do NOT tell them to click
  a "Login" link in a website menu or describe an external login page; the
  action:"login" / action:"register" CTAs open the in-app form. Keep it to one
  short step, do not repeat the same login instructions across turns.
- If the user is already signed in (their profile name is not "Guest Investor"),
  do NOT ask them to log in; use get_service_status directly.
- KYC COMPLETION: This app can complete KYC itself via the complete_kyc tool.
  When a signed-in user wants to complete/start/do their KYC, CALL complete_kyc
  and confirm the result in ONE turn. Do NOT describe a multi-step "video KYC"
  process, do NOT say you are "transferring to a specialist", and never ask
  "are you ready?" repeatedly. If the result is login_required, offer the
  "login"/"register" CTAs instead.
- If the user explicitly wants a human, or asks for personalised advice you
  cannot give, call handoff_to_advisor.
- Match the user's language (reply in Hinglish if they wrote Hinglish).
- Keep WhatsApp replies concise; emails can be longer.

Respond ONLY with a single, flat JSON object of exactly this shape:
{{"answer": "<markdown text for the user>", "ctas": [{{"label": "...", "action": "...", "url": "..."}}], "intent": "information|transaction|service|advice"}}
The "answer" value must be plain human-readable text, NOT another JSON object.
Do not nest JSON inside the answer. Do not wrap the response in code fences."""


# --------------------------------------------------------------------------- #
# Tool specifications advertised to the model (Bedrock Converse toolConfig)
# --------------------------------------------------------------------------- #
def _tool_config() -> dict[str, Any]:
    def spec(name: str, desc: str, props: dict[str, Any], required: list[str]) -> dict[str, Any]:
        return {
            "toolSpec": {
                "name": name,
                "description": desc,
                "inputSchema": {"json": {"type": "object", "properties": props, "required": required}},
            }
        }

    return {
        "tools": [
            spec(
                "search_fund_docs",
                "Semantic search over Axis MF factsheets, SIDs and FAQs. Use for any fund or service question. Each result includes a real 'fund_url' (fund page on axismf.com) and 'pdf_url' (SID/factsheet) you MUST reuse verbatim for CTA links.",
                {
                    "query": {"type": "string", "description": "The search query"},
                    "top_k": {"type": "integer", "description": "Number of results (default 3)"},
                },
                ["query"],
            ),
            spec(
                "get_service_status",
                "Look up a user's SIP, redemption or KYC status (synthetic data).",
                {
                    "user_id": {"type": "string"},
                    "service": {"type": "string", "description": "one of: kyc, sip, redemption"},
                },
                ["user_id", "service"],
            ),
            spec(
                "complete_kyc",
                "Complete (verify) the logged-in user's KYC. Call this when the user wants to complete/do/start their KYC. Returns a definitive result; do not keep asking if they are ready.",
                {"user_id": {"type": "string"}},
                ["user_id"],
            ),
            spec(
                "handoff_to_advisor",
                "Queue a hand-off to a human advisor and alert them. Use when the user wants a human or needs advice you cannot give.",
                {
                    "user_id": {"type": "string"},
                    "reason": {"type": "string"},
                    "summary": {"type": "string", "description": "Short conversation summary for the advisor"},
                },
                ["user_id", "reason", "summary"],
            ),
        ]
    }


_TOOL_IMPLS = {
    "search_fund_docs": lambda a: toolbox.search_fund_docs(a["query"], int(a.get("top_k", 3))),
    "get_service_status": lambda a: toolbox.get_service_status(a["user_id"], a["service"]),
    "complete_kyc": lambda a: toolbox.complete_kyc(a["user_id"]),
    "handoff_to_advisor": lambda a: toolbox.handoff_to_advisor(a["user_id"], a["reason"], a["summary"]),
}

# Tools that operate on a specific investor; their user_id is always forced to
# the authenticated caller, regardless of what the model supplies.
_USER_SCOPED_TOOLS = {"get_service_status", "complete_kyc", "handoff_to_advisor"}


def _profile_context(profile: dict[str, Any], channel: str, page: str | None) -> str:
    lines = [
        f"Investor name: {profile.get('name')}",
        f"Channel: {channel}",
        f"Preferred language: {profile.get('language')}",
        f"Risk tolerance: {profile.get('risk_tolerance')}",
        f"Investment goals: {', '.join(profile.get('investment_goals') or []) or 'not set'}",
        f"KYC status: {profile.get('kyc_status')}",
        f"Current holdings: {', '.join(profile.get('holdings') or []) or 'none'}",
    ]
    if page:
        lines.append(f"Current page/context: {page}")
    return "User context:\n" + "\n".join(lines)


def _parse_model_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[-1] if "\n" in text else text
    start, end = text.find("{"), text.rfind("}")
    parsed: dict[str, Any] | None = None
    if start != -1 and end != -1:
        candidate = text[start : end + 1]
        # strict=False tolerates literal control chars (e.g. raw newlines)
        # that the model sometimes emits inside JSON string values.
        try:
            parsed = json.loads(candidate, strict=False)
        except json.JSONDecodeError:
            parsed = None
    if parsed is None or not isinstance(parsed, dict):
        return {"answer": text, "ctas": [], "intent": "information"}

    # The model sometimes nests the real JSON payload inside the "answer"
    # field as a string. Unwrap one level if detected.
    answer = parsed.get("answer")
    if isinstance(answer, str) and answer.strip().startswith("{") and "\"answer\"" in answer:
        try:
            inner = json.loads(answer, strict=False)
            if isinstance(inner, dict) and "answer" in inner:
                parsed = inner
        except json.JSONDecodeError:
            pass

    parsed.setdefault("ctas", [])
    parsed.setdefault("intent", "information")
    return parsed


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def run_agent(user_id: str, message: str, channel: str = "web", page: str | None = None) -> dict[str, Any]:
    settings = get_settings()
    memory = get_memory()
    profile = memory.get_profile(user_id)

    # Guardrail: mask PII before the message reaches the model / memory.
    safe_message = mask_pii(message)
    memory.append(user_id, "user", safe_message)

    context = _profile_context(profile, channel, page)
    history = memory.get_history(user_id)

    # Deterministic hand-off: an explicit "talk to a human / advisor" request
    # must ALWAYS queue a hand-off and confirm it, independent of the model.
    if _wants_human(safe_message):
        result = _do_handoff(user_id, safe_message, profile)
    elif _wants_kyc_completion(safe_message):
        result = _do_complete_kyc(user_id, profile)
    else:
        try:
            result = _run_bedrock_loop(user_id, safe_message, context, history)
        except Exception as exc:  # noqa: BLE001 - never dead-end the demo
            logger.warning("Bedrock loop failed (%s); using local fallback.", exc)
            result = _fallback(safe_message)

        if not (result.get("answer") or "").strip():
            logger.info("Empty answer from model; using local fallback.")
            fb = _fallback(safe_message)
            result = {**fb, **{k: v for k, v in result.items() if k in ("tool_trace",)}}

    result["ctas"] = _sanitize_ctas(result.get("ctas", []), _allowed_urls(result))
    _ensure_source_cta(result)
    result["answer"] = apply_disclaimer(result.get("answer", ""))
    memory.append(user_id, "assistant", result["answer"])
    result["channel"] = channel
    result["user_id"] = user_id
    return result


_HANDOFF_RE = re.compile(r"advisor|handoff|hand-off|human|representative|talk to", re.I)
# Only links on the official Axis MF domains are allowed to be navigable.
_ALLOWED_URL_RE = re.compile(r"^https://([a-z0-9-]+\.)*axismf\.com/", re.I)


@lru_cache
def _kb_url_whitelist() -> frozenset[str]:
    """Every real fund/PDF URL from the knowledge base corpus."""
    import json as _json
    from pathlib import Path as _Path

    kb = _Path(__file__).resolve().parent / "data" / "knowledge_base.json"
    with open(kb, encoding="utf-8") as fh:
        docs = _json.load(fh)["documents"]
    urls: set[str] = set()
    for d in docs:
        for key in ("fund_url", "pdf_url"):
            if d.get(key):
                urls.add(d[key])
    return frozenset(urls)


def _ensure_source_cta(result: dict[str, Any]) -> None:
    """If no CTA carries a real link, attach a "Learn more on axismf.com" CTA
    pointing at the top retrieved document's real page, so the user always has
    a working source link (and never just dead in-app buttons)."""
    ctas = result.get("ctas", [])
    if any(c.get("url") for c in ctas):
        return
    for t in result.get("tool_trace", []):
        for r in (t.get("output", {}).get("results") or []):
            url = r.get("fund_url") or r.get("pdf_url")
            if url:
                ctas.append({"label": "Learn more on axismf.com", "action": "open", "url": url})
                result["ctas"] = ctas
                return


def _allowed_urls(result: dict[str, Any]) -> set[str]:
    """URLs the agent is permitted to link to this turn: the KB whitelist plus
    any URLs surfaced by search_fund_docs tool calls."""
    allowed = set(_kb_url_whitelist())
    for t in result.get("tool_trace", []):
        out = t.get("output", {})
        for r in (out.get("results") or []):
            for key in ("fund_url", "pdf_url"):
                if r.get(key):
                    allowed.add(r[key])
    return allowed


def _sanitize_ctas(ctas: list[dict[str, Any]], allowed_urls: set[str]) -> list[dict[str, Any]]:
    """Normalise model-generated CTAs so they never point at invented pages.

    - Advisor/hand-off CTAs become in-app actions with no URL (so the client
      triggers handoff_to_advisor instead of navigating away).
    - A URL is kept as a navigable link only if it is an official axismf.com
      URL AND it was one of the real links we supplied from the knowledge base
      this turn. Anything else (invented/hallucinated) is dropped and the label
      becomes an in-app prompt the user can click to re-ask.
    """
    clean: list[dict[str, Any]] = []
    for c in ctas or []:
        if not isinstance(c, dict):
            continue
        label = str(c.get("label", "")).strip() or "Learn more"
        action = str(c.get("action", "")).strip()
        url = str(c.get("url", "")).strip()

        if _HANDOFF_RE.search(f"{action} {label}"):
            clean.append({"label": label, "action": "handoff", "url": ""})
            continue
        if url and _ALLOWED_URL_RE.match(url) and url in allowed_urls:
            clean.append({"label": label, "action": action or "open", "url": url})
        else:
            # Invented/unverified URL -> keep as a clickable in-app prompt.
            clean.append({"label": label, "action": action or "ask", "url": ""})
    return clean


def _run_bedrock_loop(user_id: str, message: str, context: str, history: list[dict[str, str]]) -> dict[str, Any]:
    client = bedrock_runtime()
    settings = get_settings()

    messages: list[dict[str, Any]] = []
    for turn in history[:-1]:  # prior turns (exclude the just-added user msg)
        messages.append({"role": turn["role"], "content": [{"text": turn["content"]}]})
    messages.append({"role": "user", "content": [{"text": f"{context}\n\nUser message: {message}"}]})

    tool_trace: list[dict[str, Any]] = []

    for _ in range(MAX_TOOL_ITERATIONS):
        resp = client.converse(
            modelId=settings.LLM_MODEL,
            system=[{"text": SYSTEM_PROMPT}],
            messages=messages,
            toolConfig=_tool_config(),
            inferenceConfig={"maxTokens": 1024, "temperature": 0.2},
        )
        out = resp["output"]["message"]
        messages.append(out)

        tool_uses = [b["toolUse"] for b in out.get("content", []) if "toolUse" in b]
        if not tool_uses:
            text = "".join(b.get("text", "") for b in out.get("content", []))
            parsed = _parse_model_json(text)
            parsed["tool_trace"] = tool_trace
            return parsed

        tool_results = []
        for tu in tool_uses:
            name, args = tu["name"], tu.get("input", {})
            # Never trust the model's identity claim: force the real, logged-in
            # user_id on every tool that takes one. This fixes mismatches (e.g.
            # the model passing "919..." instead of "+919...") and prevents the
            # agent from querying another user's data.
            if "user_id" in args or name in _USER_SCOPED_TOOLS:
                args["user_id"] = user_id
            try:
                output = _TOOL_IMPLS[name](args)
            except Exception as exc:  # noqa: BLE001
                output = {"error": str(exc)}
            tool_trace.append({"tool": name, "input": args, "output": output})
            tool_results.append(
                {
                    "toolResult": {
                        "toolUseId": tu["toolUseId"],
                        "content": [{"json": output}],
                    }
                }
            )
        messages.append({"role": "user", "content": tool_results})

    # Exhausted iterations; return the best-effort fallback.
    result = _fallback(message)
    result["tool_trace"] = tool_trace
    return result


_WANTS_HUMAN_RE = re.compile(
    r"\b("
    r"talk to (a |an )?(human|advisor|agent|representative|person)"
    r"|connect me (with|to) (a |an )?(human|advisor|agent|representative|person)"
    r"|speak (to|with) (a |an )?(human|advisor|agent|representative|person)"
    r"|human (advisor|agent|representative|support)"
    r"|contact (a |an )?advisor"
    r"|(reach|need|want) (a |an |to talk to )?(human|advisor|agent)"
    r")\b",
    re.I,
)


def _wants_human(message: str) -> bool:
    return bool(_WANTS_HUMAN_RE.search(message or ""))


def _do_handoff(user_id: str, message: str, profile: dict[str, Any]) -> dict[str, Any]:
    """Queue a human-advisor hand-off deterministically and confirm to the user."""
    summary = (
        f"Investor {profile.get('name', user_id)} ({profile.get('channel', 'web')}) "
        f"requested to speak with a human advisor. Latest message: {message}"
    )
    ticket = toolbox.handoff_to_advisor(
        user_id=user_id,
        reason="User requested a human advisor",
        summary=summary,
    )
    ticket_id = ticket.get("ticket_id", "your request")
    answer = (
        f"Sure — I've connected you with our advisory team. Your request "
        f"(**{ticket_id}**) is queued and an Axis MF advisor will reach out to you "
        f"shortly. Is there anything else I can help you with in the meantime?"
    )
    return {
        "answer": answer,
        "ctas": [
            {"label": "Ask another question", "action": "ask", "url": ""},
            {"label": "View our funds", "action": "open", "url": "https://www.axismf.com/mutual-funds"},
        ],
        "intent": "service",
        "tool_trace": [{"tool": "handoff_to_advisor", "input": {"user_id": user_id}, "output": ticket}],
    }


_KYC_COMPLETE_RE = re.compile(
    r"\b("
    r"complete (my )?kyc|do (my )?kyc|finish (my )?kyc|start (my )?kyc"
    r"|(start|begin|do) (the )?(video )?kyc"
    r"|video kyc|e-?kyc|verify (my )?kyc|kyc verification"
    r"|proceed with kyc|get kyc done"
    r")\b",
    re.I,
)
# Phrases that are about *checking* status, not completing - exclude these.
_KYC_STATUS_RE = re.compile(r"\b(check|status|track|what.?s) .*kyc|kyc status\b", re.I)


def _wants_kyc_completion(message: str) -> bool:
    m = message or ""
    if _KYC_STATUS_RE.search(m):
        return False
    return bool(_KYC_COMPLETE_RE.search(m))


def _do_complete_kyc(user_id: str, profile: dict[str, Any]) -> dict[str, Any]:
    """Deterministically complete (demo) KYC and confirm - no 'are you ready?' loop."""
    out = toolbox.complete_kyc(user_id)
    status = out.get("status")

    if status == "login_required":
        answer = (
            "To complete your KYC you'll need to be signed in. Please log in or "
            "create an account, then I can verify your KYC instantly."
        )
        ctas = [
            {"label": "Login", "action": "login", "url": ""},
            {"label": "Register", "action": "register", "url": ""},
        ]
    elif out.get("already"):
        answer = "Good news — your KYC is already **Verified**. You're all set to invest."
        ctas = [
            {"label": "View our funds", "action": "open", "url": "https://www.axismf.com/mutual-funds"},
            {"label": "Start a SIP", "action": "ask", "url": ""},
        ]
    else:
        ref = out.get("reference", "")
        answer = (
            f"Done! Your KYC is now **Verified** (ref **{ref}**) via e-KYC using "
            f"PAN + Aadhaar. You can start investing right away."
        )
        ctas = [
            {"label": "View our funds", "action": "open", "url": "https://www.axismf.com/mutual-funds"},
            {"label": "Start a SIP", "action": "ask", "url": ""},
        ]

    return {
        "answer": answer,
        "ctas": ctas,
        "intent": "service",
        "tool_trace": [{"tool": "complete_kyc", "input": {"user_id": user_id}, "output": out}],
    }


def _fallback(message: str) -> dict[str, Any]:
    """Deterministic answer from the local KB when the model is unavailable."""
    hits = search_fund_docs(message, top_k=2)
    ctas: list[dict[str, str]] = []
    if hits:
        top = hits[0]
        answer = (
            f"Here's what I found for **{top['fund']}** ({top['category']}):\n\n"
            f"{top['snippet']}"
        )
        ctas = [
            {"label": "View fund page", "action": "open", "url": top.get("fund_url", top["pdf_url"])},
            {"label": "Download SID / factsheet", "action": "download", "url": top["pdf_url"]},
            {"label": "Talk to an advisor", "action": "handoff", "url": ""},
        ]
        intent = "information"
    else:
        answer = (
            "I couldn't find a specific document for that. I can help with Axis "
            "fund information, SIPs, KYC, redemptions, or connect you with an advisor."
        )
        ctas = [{"label": "Talk to an advisor", "action": "handoff", "url": ""}]
        intent = "service"
    return {"answer": answer, "ctas": ctas, "intent": intent, "fallback": True}
