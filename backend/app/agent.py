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
  your final answer's "ctas" field, e.g. "View fund", "Start SIP",
  "Download factsheet", "Check KYC status", "Talk to an advisor".
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
                "Semantic search over Axis MF factsheets, SIDs and FAQs. Use for any fund or service question.",
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
    "handoff_to_advisor": lambda a: toolbox.handoff_to_advisor(a["user_id"], a["reason"], a["summary"]),
}


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

    try:
        result = _run_bedrock_loop(user_id, safe_message, context, history)
    except Exception as exc:  # noqa: BLE001 - never dead-end the demo
        logger.warning("Bedrock loop failed (%s); using local fallback.", exc)
        result = _fallback(safe_message)

    if not (result.get("answer") or "").strip():
        logger.info("Empty answer from model; using local fallback.")
        fb = _fallback(safe_message)
        result = {**fb, **{k: v for k, v in result.items() if k in ("tool_trace",)}}

    result["answer"] = apply_disclaimer(result.get("answer", ""))
    memory.append(user_id, "assistant", result["answer"])
    result["channel"] = channel
    result["user_id"] = user_id
    return result


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
            args.setdefault("user_id", user_id)
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
            {"label": "Download factsheet", "action": "download", "url": top["pdf_url"]},
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
