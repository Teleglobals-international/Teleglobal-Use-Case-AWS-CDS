# Architecture & Design

A reference implementation of the **Mutual Fund Investor Assistant — Agentic
WhatsApp + Email Self-Service on AWS** described in the CDS architecture and a
"Search + Chatbot" PRD.

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
| AgentCore Memory | `backend/app/memory.py` — per-user, cross-channel context (Cognito-backed profiles) |
| Bedrock Guardrails | `backend/app/guardrails.py` — PII masking, no-advice policy, disclaimers |
| Knowledge Bases for Bedrock | `backend/app/knowledge_base.py` (+ local corpus fallback) |
| User identity / auth | `backend/app/auth.py` → **Amazon Cognito** (register/confirm/login/forgot) |
| End User Messaging Social (WhatsApp out) | `tools.send_whatsapp()` → `socialmessaging` client |
| Amazon SES (email out, live) | `tools.send_email()` → `sesv2:SendEmail` |
| Service API (mock) | `tools.get_service_status()` |
| KYC request queue | `tools.request_kyc()` — raises a queued request + ops alert |
| DynamoDB hand-off queue | `tools.handoff_to_advisor()` → `dynamodb` resource |
| Inbound WhatsApp (EUMS → SNS) | `POST /webhooks/whatsapp` + `router.normalise_whatsapp` |
| Inbound Email (SES → S3) | `POST /webhooks/email` + `router.normalise_email` |

## 3. The agent loop

1. Router normalises the inbound message to `{user_id, text, channel, page}`.
2. `run_agent` masks PII, loads the investor profile + recent history (Memory),
   and builds a context block.
3. **Deterministic intents run first** (independent of the model, for
   reliability): "talk to a human" → `handoff_to_advisor`; "complete KYC" →
   `request_kyc` (queued, "In progress", never auto-verified); "email me this"
   → a live SES summary to the signed-in user.
4. Otherwise the **Converse** call advertises the tool specs. The model may call
   `search_fund_docs`, `get_service_status`, `request_kyc`, or
   `handoff_to_advisor`. Tool results are fed back; the loop runs up to
   `MAX_TOOL_ITERATIONS`. User-scoped tools always use the authenticated
   `user_id` (the model's id claim is ignored).
5. The final model turn returns a flat JSON object `{answer, ctas, intent}`. A
   tolerant parser unwraps/repairs model JSON (including truncated output) so
   the user never sees raw JSON; it falls back to a deterministic KB answer if
   the model is unavailable or empty.
6. CTA URLs are sanitised — only real fund-house links retrieved from the KB are
   kept navigable; invented URLs become in-app actions.
7. Guardrails append the compliance disclaimer. The turn is saved to Memory.
8. For WhatsApp/Email webhooks, the reply is also pushed back out via
   `send_whatsapp` / `send_email`.

## 4. Graceful degradation (why it always runs)

The app is designed to run end-to-end on a laptop with only AWS Bedrock access:

- **Knowledge Base** — uses `KNOWLEDGE_BASE_ID` when set; otherwise a local
  keyword-scored search over `data/knowledge_base.json`.
- **Channels** — `send_email` (live via `sesv2:SendEmail`) and `send_whatsapp`
  call real boto3 clients when `SIMULATE_CHANNELS=false`; otherwise they record
  the intended message in an in-memory **OUTBOX** that the UI surfaces. Live SES
  failures are recorded with the error (not silently hidden).
- **Auth** — uses **Amazon Cognito** when `COGNITO_USER_POOL_ID`/`CLIENT_ID` are
  set; otherwise an in-memory auth store (seed demo accounts, no password) so
  the app runs without Cognito.
- **DynamoDB** — the advisor hand-off persists to DynamoDB when available, else
  an in-memory queue.
- **Model** — if Bedrock is unreachable, a deterministic fallback answers from
  the local corpus.

## 4a. Authentication (Amazon Cognito)

`auth.py` wraps a Cognito user pool:

- **Register** → `SignUp` (Cognito emails a 6-digit code). Investor profile
  fields are stored as user attributes (`name`, `custom:language`,
  `custom:risk_tolerance`, `custom:investment_goals`, `custom:kyc_status`).
- **Confirm** → `ConfirmSignUp` with the emailed code, then auto-login.
- **Login** → `InitiateAuth` (USER_PASSWORD_AUTH).
- **Forgot / reset** → `ForgotPassword` + `ConfirmForgotPassword`.
- **KYC status** persists via `AdminUpdateUserAttributes`, so it survives
  restarts. `memory.py` reads/writes profiles through Cognito for email users.

Seed demo accounts (`+91…`, `investor2@example.com`) use the local fallback so
they work without Cognito.

## 5. Confidential data sources (KYC, identity, transactions)

Confidential, investor-specific details are **synthetic in this demo**. They are
never real in this codebase and are not fetched from any live system here.

In production these details MUST come from authoritative, consent-based sources
only — not from any data generated or stored by this assistant:

| Data | Demo (now) | Production source |
|------|------------|-------------------|
| KYC status & completion | In-memory flag on a synthetic profile | An **identity/KYC provider**: consent-based e-KYC / issued-documents APIs (PAN + Aadhaar + address proof), persisted via the fund house CKYC/KRA |
| PAN / Aadhaar / identity docs | Never collected | Shared by the investor **through the identity/KYC provider** with explicit consent; only the verification result is returned |
| SIP / redemption / folio status | Synthetic sample responses | Fund house transaction APIs |
| Investor profile / holdings | `data/investors.json` (synthetic) | Fund house systems of record |

Key principles for the production integration:

- **Identity/KYC provider first for identity/KYC.** The investor authenticates
  with the provider and consents to share documents; the assistant receives only
  the verified outcome (e.g. "KYC verified"), never raw PAN/Aadhaar numbers.
- **Consent + least data.** Pull the minimum needed, per request, with an
  auditable consent artefact. Do not cache confidential PII in this service.
- **Fund house transaction APIs for money movement and account state** (SIP,
  redemption, folio, bank mandate), called server-side with scoped credentials.

KYC is never self-verified in this app: `tools.request_kyc()` raises a queued
request, marks the profile "In progress", and alerts the ops team — mirroring a
real consent-based e-KYC that a verification team/provider completes. The
relevant code (`tools.get_service_status`, `tools.request_kyc`) carries a
`PRODUCTION NOTE` marking exactly where these integrations plug in.

## 6. Security & compliance notes

- Credentials are read only from the repo-root `.env`; never hard-coded and
  never sent to the browser (the Node layer proxies all API calls).
- PII (PAN, Aadhaar, account numbers, phone, email) is masked before the text
  reaches the model or logs.
- Confidential investor data (KYC/identity/transactions) is synthetic here; in
  production it is sourced from an identity/KYC provider and the fund house
  transaction APIs (see section 5).
- The agent is constrained by a policy that forbids personalised investment
  advice and predicted returns; such requests are routed to a human advisor.
- Every answer carries the statutory market-risk disclaimer.
- All seed investor/fund data is **synthetic**.

## 7. IAM permissions (by feature)

The runtime principal needs these actions (scope `Resource` down in production):

| Feature | Actions |
|---------|---------|
| LLM (required) | `bedrock:InvokeModel` (Converse) for the configured model |
| Knowledge Base (optional) | `bedrock:Retrieve` on the KB |
| Email (live SES) | `ses:SendEmail` (sender identity verified in-Region) |
| Auth (Cognito) | `cognito-idp:SignUp`, `ConfirmSignUp`, `InitiateAuth`, `ForgotPassword`, `ConfirmForgotPassword`, `ResendConfirmationCode`, `AdminGetUser`, `AdminUpdateUserAttributes` |
| Hand-off queue (optional) | `dynamodb:PutItem` on `HANDOFF_TABLE` |
| WhatsApp (optional) | `social-messaging:SendWhatsAppMessage` |

Missing a permission is non-fatal — the corresponding feature degrades to its
simulated/in-memory fallback.

## 8. Production hardening (next steps)

- Deploy the backend as an AgentCore Runtime / Lambda behind API Gateway.
- Provision a real Knowledge Base (S3 + embeddings) and set `KNOWLEDGE_BASE_ID`.
- Wire the WhatsApp Business Account + SES receiving domain; set
  `SIMULATE_CHANNELS=false`, `WHATSAPP_PHONE_NUMBER_ID`, verified `SES_FROM_EMAIL`.
- Wire Cognito verification/reset emails through SES for branded, uncapped mail.
- Create the DynamoDB hand-off table (`HANDOFF_TABLE`).
- Attach a managed Bedrock Guardrail for defence in depth.
- Add IAM least-privilege roles, KMS, CloudWatch + AgentCore Observability, CDK.
