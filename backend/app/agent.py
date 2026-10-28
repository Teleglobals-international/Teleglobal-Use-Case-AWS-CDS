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

SYSTEM_PROMPT = f"""You are the Mutual Fund Investor Assistant, a helpful,
accurate, multilingual (English + Hinglish) assistant for mutual fund investors
across WhatsApp, Email and the fund house website/app.

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
  construct links yourself (e.g. a made-up "talk-to-advisor" page).
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
- KYC: KYC is NOT verified instantly and you must NEVER claim it is verified.
  When a signed-in user wants to complete/start/do their KYC, CALL request_kyc
  (once) to raise a request into the queue, then tell them it's "in progress"
  and that our verification team will reach out to complete the e-KYC. Do NOT
  describe a "video KYC" walkthrough, do NOT say "transferring to a specialist",
  and never ask "are you ready?" repeatedly. If the result is login_required,
  offer the "login"/"register" CTAs instead.
- EMAIL: A signed-in user can have any answer emailed to them. When relevant
  (e.g. detailed fund info, SIP steps, a summary), offer a CTA with
  action "email" and label "Email me this" and empty url. Do NOT claim you have
  already emailed something unless the email tool result says so.
- If the user explicitly wants a human, or asks for personalised advice you
  cannot give, call handoff_to_advisor.
- Match the user's language (reply in Hinglish if they wrote Hinglish).
- Keep WhatsApp replies concise; emails can be longer.

Respond ONLY with a single, flat JSON object of exactly this shape:
{{"answer": "<markdown text for the user>", "ctas": [{{"label": "...", "action": "...", "url": "..."}}], "intent": "information|transaction|service|advice"}}
The "answer" value must be plain human-readable text, NOT another JSON object.
Do not nest JSON inside the answer. Do not wrap the response in code fences.
Do NOT emit any preamble like "I'll search the knowledge base" — call the tool
silently, then return ONLY the final, complete JSON object after the tool runs."""


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
                "Semantic search over fund factsheets, SIDs and FAQs. Use for any fund or service question. Each result includes a 'fund_url' (fund page) and 'pdf_url' (SID/factsheet) you MUST reuse verbatim for CTA links.",
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
                "request_kyc",
                "Raise a KYC verification request into the queue for the logged-in user. Call this when the user wants to complete/do/start their KYC. KYC is NOT verified instantly — the team processes it; status becomes 'In progress'. Do not claim the KYC is verified.",
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
    "request_kyc": lambda a: toolbox.request_kyc(a["user_id"]),
    "handoff_to_advisor": lambda a: toolbox.handoff_to_advisor(a["user_id"], a["reason"], a["summary"]),
}

# Tools that operate on a specific investor; their user_id is always forced to
# the authenticated caller, regardless of what the model supplies.
_USER_SCOPED_TOOLS = {"get_service_status", "request_kyc", "handoff_to_advisor"}


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


def _recover_answer_text(text: str) -> str:
    """Best-effort extraction of the human answer from malformed/truncated JSON.

    Handles cases like:  {"answer": "I'll search ...\n\n   (no closing brace)
    Falls back to stripping obvious JSON scaffolding so the user never sees raw
    JSON like '{"answer": "...'.
    """
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t.split("\n", 1)[-1] if "\n" in t else t
        t = t.strip()

    # If it looks like a JSON object with an "answer" field, pull that value.
    m = re.search(r'"answer"\s*:\s*"', t)
    if m:
        rest = t[m.end():]
        # Read until an unescaped closing quote (or end of string if truncated).
        out, i = [], 0
        while i < len(rest):
            ch = rest[i]
            if ch == "\\" and i + 1 < len(rest):
                nxt = rest[i + 1]
                out.append({"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/"}.get(nxt, nxt))
                i += 2
                continue
            if ch == '"':
                break
            out.append(ch)
            i += 1
        recovered = "".join(out).strip()
        if recovered:
            return recovered

    # Not recoverable as JSON — strip any leftover braces/keys defensively.
    return t


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
        # The model emitted something that looks like JSON but is malformed or
        # truncated (e.g. a streamed opening "{\"answer\": \"...\"" with no
        # closing brace). Rather than dump raw JSON to the user, try to recover
        # the human-readable answer string.
        recovered = _recover_answer_text(text)
        return {"answer": recovered, "ctas": [], "intent": "information"}

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
    elif _wants_email(safe_message):
        result = _do_email_this(user_id, profile, history)
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
# Only links on the configured fund-house domain are allowed to be navigable.
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
    """If no CTA carries a real link, attach a "Learn more" CTA pointing at the
    top retrieved document's page, so the user always has a working source link
    (and never just dead in-app buttons)."""
    ctas = result.get("ctas", [])
    if any(c.get("url") for c in ctas):
        return
    for t in result.get("tool_trace", []):
        for r in (t.get("output", {}).get("results") or []):
            url = r.get("fund_url") or r.get("pdf_url")
            if url:
                ctas.append({"label": "Learn more", "action": "open", "url": url})
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
    - A URL is kept as a navigable link only if it is a fund-house domain
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
        f"(**{ticket_id}**) is queued and an advisor will reach out to you "
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
    """Raise a KYC request into the queue and confirm - no instant self-verify."""
    out = toolbox.request_kyc(user_id)
    status = out.get("status")

    if status == "login_required":
        answer = (
            "To start your KYC you'll need to be signed in. Please log in or "
            "create an account, then I can raise a KYC request for you."
        )
        ctas = [
            {"label": "Login", "action": "login", "url": ""},
            {"label": "Register", "action": "register", "url": ""},
        ]
    elif status == "Verified" and out.get("already"):
        answer = "Good news — your KYC is already **Verified**. You're all set to invest."
        ctas = [
            {"label": "View our funds", "action": "open", "url": "https://www.axismf.com/mutual-funds"},
            {"label": "Start a SIP", "action": "ask", "url": ""},
        ]
    elif status == "In progress" and out.get("already"):
        ref = out.get("reference", "")
        answer = (
            "Your KYC request is already **in progress**. Our verification team "
            "will reach out to you shortly to complete the e-KYC. You'll be able "
            "to invest once it's verified."
        )
        ctas = [
            {"label": "Check KYC status", "action": "ask", "url": ""},
            {"label": "Talk to an advisor", "action": "handoff", "url": ""},
        ]
    else:
        ref = out.get("reference", "")
        answer = (
            f"Thanks — I've raised your KYC request (ref **{ref}**) and it's now "
            f"**in progress**. Our verification team will reach out to you shortly "
            f"to complete the e-KYC (PAN + Aadhaar). You'll be ready to invest once "
            f"it's verified; I'll keep you posted."
        )
        ctas = [
            {"label": "Learn about mutual funds", "action": "ask", "url": ""},
            {"label": "Talk to an advisor", "action": "handoff", "url": ""},
        ]

    return {
        "answer": answer,
        "ctas": ctas,
        "intent": "service",
        "tool_trace": [{"tool": "request_kyc", "input": {"user_id": user_id}, "output": out}],
    }


_WANTS_EMAIL_RE = re.compile(
    r"\b("
    r"email (me|this|that|it)|e-?mail (me|this|that|it)"
    r"|send (me )?(this|that|it|the (details|info|summary)) (on|via|to|by|over)? ?e?-?mail"
    r"|(share|send) (this|that|it|the above) (on|via|by|to|over) e?-?mail"
    r"|mail (me|this|that|it)"
    r")\b",
    re.I,
)


def _wants_email(message: str) -> bool:
    return bool(_WANTS_EMAIL_RE.search(message or ""))


def _resolve_email(user_id: str, profile: dict[str, Any]) -> str | None:
    """Determine the recipient email for the signed-in user."""
    if "@" in (user_id or ""):
        return user_id
    email = profile.get("email")
    if email and "@" in email:
        return email
    return None


# Turns that are NOT worth emailing: promises to email, pure KYC/login/hand-off
# nudges, or the email-confirmation itself. We skip these and email the last
# turn that actually contains useful information.
_FILLER_RE = re.compile(
    r"i'?ll send you an email"
    r"|i'?ve sent the details"
    r"|email with (comprehensive|detailed)"
    r"|would you like to complete your kyc"
    r"|to email this to you"
    r"|connected you with our advisory",
    re.I,
)


def _strip_disclaimer(text: str) -> str:
    idx = text.find("\n\n---")
    return (text[:idx] if idx != -1 else text).strip()


def _last_assistant_answer(history: list[dict[str, str]]) -> str | None:
    """Pick the most recent SUBSTANTIVE assistant turn to email.

    Skips filler turns (promises to email, KYC/login nudges, confirmations) and
    very short replies. Falls back to the most recent non-empty answer if every
    prior turn looks like filler.
    """
    assistant_turns = [
        t["content"] for t in history
        if t.get("role") == "assistant" and t.get("content", "").strip()
    ]
    if not assistant_turns:
        return None
    for content in reversed(assistant_turns):
        core = _strip_disclaimer(content)
        if len(core) >= 60 and not _FILLER_RE.search(core):
            return content
    # Everything looked like filler — return the most recent non-empty turn.
    return assistant_turns[-1]


_SUMMARY_SYSTEM = (
    "You write a concise, friendly email for a mutual fund investor summarising "
    "a chat with the assistant. The email MUST include the concrete FUND DETAILS "
    "from the 'Fund facts' provided (scheme type, objective, benchmark, minimum "
    "SIP, lock-in, exit load, risk level) for the fund(s) the investor asked "
    "about — quote the real figures, do not invent any. Structure: a one-line "
    "greeting; a short line on what they asked; a 'Fund details' section with "
    "bullet points of the real facts; any relevant next step (e.g. completing "
    "KYC) if it came up. Plain text only (no markdown headers, no JSON). Do NOT "
    "give personalised investment advice. Keep it under ~280 words and end with "
    "a short sign-off from 'MF Investor Assistant'."
)


def _email_fund_facts(history: list[dict[str, str]]) -> str:
    """Retrieve grounded fund facts from the knowledge base for the funds/topics
    discussed, so the email contains authoritative details (not just a recap)."""
    investor_text = " ".join(
        t.get("content", "") for t in history if t.get("role") == "user"
    )
    if not investor_text.strip():
        return ""
    hits = search_fund_docs(investor_text, top_k=3)
    blocks = []
    for h in hits:
        blocks.append(
            f"- {h['fund']} ({h['category']}): {h['snippet']}\n"
            f"  Reference: {h.get('fund_url') or h.get('pdf_url', '')}"
        )
    return "\n".join(blocks)


def _summarise_conversation(history: list[dict[str, str]], profile: dict[str, Any]) -> str:
    """Build the email body: a friendly summary grounded in real fund facts
    retrieved from the knowledge base.

    Falls back to the fund facts (or the last substantive answer) if the model
    call fails, so the email is always sent with useful content.
    """
    transcript_lines = []
    for t in history:
        role = "Investor" if t.get("role") == "user" else "Assistant"
        content = _strip_disclaimer(t.get("content", "")).strip()
        if content:
            transcript_lines.append(f"{role}: {content}")
    transcript = "\n".join(transcript_lines[-20:])  # cap context size
    fund_facts = _email_fund_facts(history)
    name = profile.get("name", "there")

    try:
        resp = bedrock_runtime().converse(
            modelId=get_settings().LLM_MODEL,
            system=[{"text": _SUMMARY_SYSTEM}],
            messages=[{
                "role": "user",
                "content": [{"text": (
                    f"Investor name: {name}\n\n"
                    f"Fund facts (authoritative — use these figures verbatim):\n{fund_facts or '(none found)'}\n\n"
                    f"Conversation:\n{transcript}\n\n"
                    "Write the email now, including the Fund details section."
                )}],
            }],
            inferenceConfig={"maxTokens": 700, "temperature": 0.2},
        )
        text = "".join(b.get("text", "") for b in resp["output"]["message"].get("content", []))
        text = text.strip()
        if text:
            return text
    except Exception as exc:  # noqa: BLE001 - never block the email
        logger.warning("Conversation summary failed (%s); using fund facts.", exc)

    # Fallback: send the grounded fund facts directly (or the last answer).
    if fund_facts:
        return f"Hi {name},\n\nHere are the fund details you asked about:\n\n{fund_facts}\n\n— MF Investor Assistant"
    fallback = _last_assistant_answer(history) or "Thanks for chatting with the MF Investor Assistant."
    return _strip_disclaimer(fallback)


def _do_email_this(user_id: str, profile: dict[str, Any], history: list[dict[str, str]]) -> dict[str, Any]:
    """Email the last assistant answer to the signed-in user's address."""
    # Must be signed in.
    if user_id == "guest" or not get_memory().exists(user_id):
        return {
            "answer": (
                "To email this to you, please sign in first so I know where to "
                "send it. You can log in or create an account in a few seconds."
            ),
            "ctas": [
                {"label": "Login", "action": "login", "url": ""},
                {"label": "Register", "action": "register", "url": ""},
            ],
            "intent": "service",
        }

    recipient = _resolve_email(user_id, profile)
    if not recipient:
        return {
            "answer": (
                "I'd be happy to email this, but your account doesn't have an "
                "email address on file (you're signed in with a mobile number). "
                "Please share the email address you'd like me to send it to, or "
                "register with an email."
            ),
            "ctas": [{"label": "Talk to an advisor", "action": "handoff", "url": ""}],
            "intent": "service",
        }

    if not any(t.get("role") == "assistant" and t.get("content", "").strip() for t in history):
        return {
            "answer": "There's nothing to email yet — ask me a question first, then say 'email me this'.",
            "ctas": [],
            "intent": "service",
        }

    # Summarise the whole conversation into a clean email body (falls back to
    # the last substantive answer if the model is unavailable).
    body = _summarise_conversation(history, profile)

    sent = toolbox.send_email(
        to=recipient,
        subject="Your mutual fund assistant summary",
        body=body,
    )
    mode = "sent" if not sent.get("simulated") else "queued (demo mode — SES not live)"
    answer = (
        f"Done — I've {mode} the details to **{recipient}**. "
        f"Please check your inbox (and spam folder just in case)."
    )
    return {
        "answer": answer,
        "ctas": [{"label": "Ask another question", "action": "ask", "url": ""}],
        "intent": "service",
        "tool_trace": [{"tool": "send_email", "input": {"to": recipient}, "output": sent}],
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
            "I couldn't find a specific document for that. I can help with "
            "fund information, SIPs, KYC, redemptions, or connect you with an advisor."
        )
        ctas = [{"label": "Talk to an advisor", "action": "handoff", "url": ""}]
        intent = "service"
    return {"answer": answer, "ctas": ctas, "intent": intent, "fallback": True}
