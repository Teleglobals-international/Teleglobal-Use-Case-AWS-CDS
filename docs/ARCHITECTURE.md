# Architecture & Design

A reference implementation of the **Mutual Fund Investor Assistant — Agentic
WhatsApp + Email Self-Service on AWS** described in the CDS architecture and the
Axis AMC "Search + Chatbot" PRD.

## 1. What this project is

A runnable, two-tier application that faithfully implements the architecture's
logic:

- **Backend (Python / FastAPI)** — the agent brain. It plays the combined role
  of the **AgentCore Runtime**, **Lambda Router**, and **AgentCore Gateway**
  from the diagram. It calls **Amazon Bedrock** (Claude 3.5 Sonnet) at runtime
  via the `bedrock-runtime` Converse API with tool use.
- **Frontend (Node.js / Express)** — the PRD's unified search + chat UI
  (search bar, AI results page, chat window, personalised homepage, intent CTAs)
  served statically and proxied to the backend.

## 2. Mapping to the CDS architecture

| Architecture component | Implementation |
|------------------------|----------------|
| AgentCore Runtime (MF Assistant agent) | `backend/app/agent.py` — Bedrock Converse tool-use loop |
| Lambda Router | `backend/app/router.py` — channel/user normalisation |
| AgentCore Gateway (tools) | `backend/app/tools.py` |
| AgentCore Memory | `backend/app/memory.py` — per-user, cross-channel context |
| Bedrock Guardrails | `backend/app/guardrails.py` — PII masking, no-advice policy, disclaimers |
| Knowledge Bases for Bedrock | `backend/app/knowledge_base.py` (+ local corpus fallback) |
| End User Messaging Social (WhatsApp out) | `tools.send_whatsapp()` → `socialmessaging` client |
| Amazon SES (email out) | `tools.send_email()` → `sesv2` client |
| Service API (mock) | `tools.get_service_status()` |
| DynamoDB hand-off queue | `tools.handoff_to_advisor()` → `dynamodb` resource |
| Inbound WhatsApp (EUMS → SNS) | `POST /webhooks/whatsapp` + `router.normalise_whatsapp` |
| Inbound Email (SES → S3) | `POST /webhooks/email` + `router.normalise_email` |

## 3. The agent loop

1. Router normalises the inbound message to `{user_id, text, channel, page}`.
2. `run_agent` masks PII, loads the investor profile + recent history (Memory),
   and builds a context block.
3. The **Converse** call advertises the tool specs. The model may call
   `search_fund_docs`, `get_service_status`, or `handoff_to_advisor`. Tool
   results are fed back; the loop runs up to `MAX_TOOL_ITERATIONS`.
4. The final model turn returns a flat JSON object
   `{answer, ctas, intent}`. A tolerant parser unwraps/repairs model JSON and
   falls back to a deterministic KB answer if the model is unavailable or empty.
5. Guardrails append the compliance disclaimer. The turn is saved to Memory.
6. For WhatsApp/Email webhooks, the reply is also pushed back out via
   `send_whatsapp` / `send_email`.

## 4. Graceful degradation (why it always runs)

The app is designed to run end-to-end on a laptop with only AWS Bedrock access:

- **Knowledge Base** — uses `KNOWLEDGE_BASE_ID` when set; otherwise a local
  keyword-scored search over `data/knowledge_base.json`.
- **Channels** — `send_whatsapp` / `send_email` / `handoff_to_advisor` call
  real boto3 clients when `SIMULATE_CHANNELS=false` and resources exist;
  otherwise they record the intended message in an in-memory **OUTBOX** that the
  UI surfaces. This lets the full flow complete without a live WhatsApp Business
  Account, SES-verified domain, or DynamoDB table.
- **Model** — if Bedrock is unreachable, a deterministic fallback answers from
  the local corpus.

## 5. Security & compliance notes

- Credentials are read only from the repo-root `.env`; never hard-coded and
  never sent to the browser (the Node layer proxies all API calls).
- PII (PAN, Aadhaar, account numbers, phone, email) is masked before the text
  reaches the model or logs.
- The agent is constrained by a policy that forbids personalised investment
  advice and predicted returns; such requests are routed to a human advisor.
- Every answer carries the statutory market-risk disclaimer.
- All seed investor/fund data is **synthetic**.

## 6. Production hardening (next steps)

- Deploy the backend as an AgentCore Runtime / Lambda behind API Gateway.
- Provision a real Knowledge Base (S3 + embeddings) and set `KNOWLEDGE_BASE_ID`.
- Wire the WhatsApp Business Account + SES receiving domain; set
  `SIMULATE_CHANNELS=false`, `WHATSAPP_PHONE_NUMBER_ID`, verified `SES_FROM_EMAIL`.
- Create the DynamoDB hand-off table (`HANDOFF_TABLE`).
- Attach a managed Bedrock Guardrail for defence in depth.
- Add IAM least-privilege roles, KMS, CloudWatch + AgentCore Observability, CDK.
