# Mutual Fund Investor Assistant

An agentic **Search + Chatbot** for mutual fund investors across **WhatsApp,
Email, and the website/app**, built on **Amazon Bedrock (AgentCore pattern)**.
It understands intent and context, answers from a fund knowledge base, generates
intent-based CTAs, enforces compliance guardrails, and can hand off to a human
advisor — following a Search + Chatbot PRD and the CDS reference architecture.

- **Backend:** Python 3.12 + FastAPI + boto3 (Amazon Bedrock Claude 3.5 Sonnet)
- **Frontend:** Node.js + Express (unified search + chat single-page UI)
- **Auth:** Amazon Cognito (email + password, email-code confirmation, forgot
  password), with an in-memory fallback for offline/demo use
- **Email:** live outbound via **Amazon SES** (`sesv2:SendEmail`) at runtime

> Synthetic data only. Provides information, not investment advice.
>
> **Confidential investor data (KYC, identity, transactions) is synthetic in
> this demo.** In production it is sourced only from authoritative,
> consent-based systems — identity/KYC via an **identity/KYC provider**
> (PAN + Aadhaar) and account/transaction data via the **fund house transaction
> APIs**. Raw PAN/Aadhaar numbers are never collected or stored by this service.
> See
> [Confidential data sources](docs/ARCHITECTURE.md#5-confidential-data-sources-kyc-identity-transactions).

---

## Table of contents
- [Features](#features)
- [Repository layout](#repository-layout)
- [Prerequisites](#prerequisites)
- [Quick start](#quick-start)
- [For reviewers / judges — how to test](#for-reviewers--judges--how-to-test)
- [How it works](#how-it-works)
- [Configuration](#configuration)
- [Testing](#testing)
- [Documentation](#documentation)

---

## Features

Mapped directly to the PRD:

- **Unified search + query bar** — one entry point on every page (web UI).
- **AI-powered results + chat window** — grounded answers with follow-up chat.
- **Personalised homepage** — recommendations from the investor's goals and
  trending queries.
- **WhatsApp + Email + Web** — one agent, three channels, shared memory.
- **NLP intent + context** — information / transaction / service / advice, with
  page-aware context (e.g. KYC help on the KYC page).
- **Knowledge base integration** — factsheets, SIDs, FAQs, with CTA links to the
  real fund-house website (every link is reachability-checked).
- **Personalisation** — per-investor profile, risk tolerance, holdings, language
  (English + Hinglish).
- **Intent-based CTAs** — "View fund page", "Download SID", "Check KYC",
  "Talk to an advisor", "Email me this".
- **Authentication (Amazon Cognito)** — register (with email verification code),
  login (email + password), and forgot/reset password. Investor profile and KYC
  status are stored as Cognito attributes, so they persist across restarts.
  A local in-memory fallback is used when Cognito is not configured.
- **Email me this (live SES)** — a signed-in user can have the assistant email
  them a concise, knowledge-base-grounded summary of the conversation. Sent
  live via Amazon SES with a real `MessageId`.
- **KYC as a queued request** — KYC is **not** self-verified instantly. The
  assistant raises a KYC request into a queue, marks it "In progress", alerts
  the ops team, and tells the investor the team will reach out (mirrors a real
  consent-based e-KYC flow).
- **Guardrails** — PII masking, no personalised advice, statutory disclaimer.
- **Human hand-off** — advisor request queued + alert email.
- **Agent activity panel** — shows real/simulated emails, KYC requests and
  advisor hand-offs produced by the agent.

> **Confidential data:** KYC/identity/transaction details are synthetic here. In
> production they come from an **identity/KYC provider** (PAN + Aadhaar, consent
> based) and the **fund house transaction APIs**; raw PAN/Aadhaar are never
> stored by this service.

## Repository layout

```
mf-investor-assistant/
├─ .env                         # AWS/Bedrock/Cognito/SES config (git-ignored; not committed)
├─ README.md
├─ backend/                     # Python FastAPI service (the agent)
│  ├─ requirements.txt
│  ├─ app/
│  │  ├─ main.py                # FastAPI app + endpoints
│  │  ├─ config.py              # settings from .env (AWS, Bedrock, Cognito, SES)
│  │  ├─ aws_clients.py         # boto3 clients (bedrock, sesv2, socialmessaging, dynamodb, cognito-idp)
│  │  ├─ auth.py                # Amazon Cognito auth (register/confirm/login/forgot)
│  │  ├─ router.py              # Lambda-router equivalent (channel normalisation)
│  │  ├─ agent.py               # AgentCore Runtime (Bedrock Converse + tools)
│  │  ├─ tools.py               # AgentCore Gateway tools (search, email, KYC, hand-off)
│  │  ├─ knowledge_base.py      # KB retrieval (Bedrock KB or local corpus)
│  │  ├─ memory.py              # per-user cross-channel memory (Cognito-backed profiles)
│  │  ├─ guardrails.py          # PII mask, policy, disclaimer
│  │  └─ data/                  # synthetic KB + investor profiles
│  └─ tests/test_smoke.py
├─ frontend/                    # Node.js Express UI
│  ├─ package.json
│  ├─ server.js                 # static server + API proxy
│  └─ public/ (index.html, styles.css, app.js)
└─ docs/
   ├─ ARCHITECTURE.md
   ├─ FLOW.md                   # flow diagrams (mermaid + PNG)
   ├─ API.md
   └─ RELEASE_NOTES.md
```

## Prerequisites

- Python 3.10+ (tested on 3.12)
- Node.js 18+ (tested on 24)
- AWS credentials (read from the repo-root `.env`) with access to:
  - **Amazon Bedrock** — the configured model, in a supported Region (required).
  - **Amazon SES** (`sesv2:SendEmail`) — for live email; a verified sender in
    your Region. Optional: without it, set `SIMULATE_CHANNELS=true`.
  - **Amazon Cognito** (`cognito-idp:*` for sign-up/login/forgot) — for real
    auth. Optional: without `COGNITO_USER_POOL_ID`/`COGNITO_CLIENT_ID`, the app
    falls back to in-memory auth.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the IAM actions each
feature needs.

## Quick start

Open **two** terminals.

### 1) Backend (Python)

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # Windows PowerShell
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

macOS/Linux: `source .venv/bin/activate` instead of the Activate.ps1 line.

Check it: open http://127.0.0.1:8000/docs or `curl http://127.0.0.1:8000/health`.

### 2) Frontend (Node.js)

```powershell
cd frontend
npm install
npm start
```

Open **http://localhost:3000**. Sign in with **Login** (top right):

- **Register** with a real email + password → enter the 6-digit code emailed by
  Cognito → you're signed in. (Password: 8+ chars, upper, lower, a number.)
- **Forgot password** sends a reset code to your email.
- Or use the **demo accounts** (no password, local fallback): Investor One
  (`+919000000001`, WhatsApp/Hinglish) or Investor Two (`investor2@example.com`,
  Email/English). Or browse as Guest.

Ask things like:

- "Tell me about the ELSS tax saver fund"
- "How do I start a SIP?" / "SIP kaise shuru karein?"
- "Explain the KYC process" → then "Complete KYC" (raises a queued request)
- "Email me this" (signed-in email users get a live SES summary)
- "I want to talk to a human advisor"

The **Agent activity** panel shows the agent's emails, KYC requests and advisor
hand-offs (live or simulated).

## For reviewers / judges — how to test

A 2-minute script that exercises the real AWS integrations end to end.

### Real email (Amazon SES) — the key "real messaging" check
Email is sent **live via `sesv2:SendEmail`** (the account has SES production
access, so it can send to any address).

1. Click **Login → Register** and sign up with **your own email** + a password
   (8+ chars, with upper, lower and a number).
2. You'll receive a **6-digit verification code** (sender:
   `support@teleglobals.com`). Enter it to confirm — you're signed in.
3. Ask a fund question, e.g. **"Tell me about the ELSS tax saver fund"**.
4. Type **"email me this"** (or click the **Email me this** CTA).
5. Check your inbox — you'll get a grounded summary from
   **`support@teleglobals.com`**. The response and the **Agent activity** panel
   show a real SES `MessageId` (not simulated).

> Prefer not to register? You can also verify SES by sending to the demo sender:
> the signed-in email account receives the summary at whatever email you
> registered with. The sender address for all outbound mail is
> **`support@teleglobals.com`**.

### Authentication (Amazon Cognito)
- **Register / confirm / login** as above (users are stored in a Cognito user
  pool; profile + KYC status persist across restarts).
- **Forgot password:** on the login screen click **Forgot password?**, enter
  your email, receive a reset code, set a new password, and you're logged in.

### Agent capabilities to try
- **KYC request (queued, realistic):** ask **"Complete KYC"** → the assistant
  raises a queued request, marks it **In progress**, and says the team will
  reach out. It does **not** instantly "verify" (by design). Ask **"check my
  KYC status"** to see it reflected.
- **Human hand-off:** **"I want to talk to a human advisor"** → a hand-off is
  queued with a ticket id (shown in the Agent activity panel).
- **Grounded answers + real links:** every fund CTA ("View fund page",
  "Download SID") opens a real, reachable fund-house page.
- **Hinglish:** log in as demo **Investor One** (`+919000000001`, no password)
  and ask **"SIP kaise shuru karein?"**.

### Channels note
- **Email is live (SES).** **WhatsApp** is wired to the same agent but runs in
  **simulation** (no live WhatsApp Business number in this demo) — inbound
  WhatsApp/email can be exercised via `POST /webhooks/whatsapp` and
  `POST /webhooks/email` (see [`docs/API.md`](docs/API.md)); outbound WhatsApp is
  recorded in the Agent activity panel.

## How it works

The browser talks only to the Node server, which proxies to the Python backend
(so credentials never reach the client). The backend runs an Amazon Bedrock
tool-using agent: it normalises the channel, loads per-user memory, applies
guardrails, retrieves fund docs from the knowledge base, calls gateway tools,
and returns a grounded answer with intent-based CTAs.

See [`docs/FLOW.md`](docs/FLOW.md) for the full flow diagram and
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the component mapping.

## Configuration

All config is read from the repo-root `.env`. Key variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `AWS_REGION` | `ap-south-1` | Region for all clients |
| `LLM_MODEL` | Claude 3.5 Sonnet (APAC) | Bedrock model id |
| `KNOWLEDGE_BASE_ID` | _(empty)_ | Use a real Bedrock Knowledge Base instead of the local corpus |
| `SIMULATE_CHANNELS` | `false` | `false` = send real email via SES; `true` = local simulation |
| `SES_FROM_EMAIL` | _(set in .env)_ | **SES-verified** sender address |
| `ADVISOR_ALERT_EMAIL` | _(set in .env)_ | Recipient for KYC/hand-off alerts |
| `COGNITO_USER_POOL_ID` | _(set in .env)_ | Cognito user pool; unset → in-memory auth |
| `COGNITO_CLIENT_ID` | _(set in .env)_ | Cognito app client id |
| `WHATSAPP_PHONE_NUMBER_ID` | _(empty)_ | End User Messaging phone id |
| `HANDOFF_TABLE` | `mf-advisor-handoff-queue` | DynamoDB table name |

Behaviour is graceful: if SES/Cognito/DynamoDB aren't available, the app
degrades (simulated email, in-memory auth, in-memory hand-off queue) so it still
runs end to end. See [`backend/.env.example`](backend/.env.example) for the full
list.

> **Security:** `.env` is git-ignored and must never be committed. Rotate any
> AWS keys that may have been exposed. Use placeholders in `.env.example` only.

## Testing

```powershell
cd backend
.\.venv\Scripts\Activate.ps1
python tests/test_smoke.py        # standalone, no extra deps
# or, if you have pytest installed:  pip install pytest && python -m pytest -q
```

The smoke test runs offline (it forces `SIMULATE_CHANNELS=true`) and exercises
the knowledge base, guardrails, PII masking, the model-JSON parser, and the
agent fallback — no network, AWS, or email sending required.

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — design and CDS mapping
- [`docs/FLOW.md`](docs/FLOW.md) — flow diagrams
- [`docs/API.md`](docs/API.md) — endpoint reference
- [`docs/RELEASE_NOTES.md`](docs/RELEASE_NOTES.md) — changelog / release notes
