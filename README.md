# Axis AMC — Mutual Fund Investor Assistant

An agentic **Search + Chatbot** for mutual fund investors across **WhatsApp,
Email, and the website/app**, built on **Amazon Bedrock (AgentCore pattern)**.
It understands intent and context, answers from a fund knowledge base, generates
intent-based CTAs, enforces compliance guardrails, and can hand off to a human
advisor — exactly as described in the Axis AMC PRD and the CDS reference
architecture.

- **Backend:** Python 3.12 + FastAPI + boto3 (Amazon Bedrock Claude 3.5 Sonnet)
- **Frontend:** Node.js + Express (unified search + chat single-page UI)

> Synthetic data only. Provides information, not investment advice.
>
> **Confidential investor data (KYC, identity, transactions) is synthetic in
> this demo.** In production it is sourced only from authoritative,
> consent-based systems — identity/KYC via **DigiLocker** (PAN + Aadhaar) and
> account/transaction data via **Axis Bank APIs**. Raw PAN/Aadhaar numbers are
> never collected or stored by this service. See
> [Confidential data sources](docs/ARCHITECTURE.md#5-confidential-data-sources-kyc-identity-transactions).

---

## Table of contents
- [Features](#features)
- [Repository layout](#repository-layout)
- [Prerequisites](#prerequisites)
- [Quick start](#quick-start)
- [How it works](#how-it-works)
- [Configuration](#configuration)
- [Testing](#testing)
- [Documentation](#documentation)

---

## Features

Mapped directly to the PRD:

- **Unified search + query bar** — one entry point on every page (web UI).
- **AI-powered results + chat window** — grounded answers with follow-up chat.
- **Personalised homepage** — recommendations from the investor's goals,
  trending queries, quick actions.
- **WhatsApp + Email + Web** — one agent, three channels, shared memory.
- **NLP intent + context** — information / transaction / service / advice, with
  page-aware context (e.g. KYC help on the KYC page).
- **Knowledge base integration** — factsheets, SIDs, FAQs.
- **Personalisation** — per-investor profile, risk tolerance, holdings, language
  (English + Hinglish).
- **Intent-based CTAs** — "Start SIP", "Download factsheet", "Check KYC",
  "Talk to an advisor".
- **Guardrails** — PII masking, no personalised advice, statutory disclaimer.
- **Human hand-off** — queue + advisor alert.
- **Login / register** — demo auth against synthetic investor profiles (plus
  in-memory registration), so the assistant has an identity for account queries.
- **KYC & service status** — synthetic in this demo. In production, confidential
  details come from **DigiLocker** (identity/KYC) and **Axis Bank APIs**
  (SIP/redemption/folio); see the note above.

## Repository layout

```
AXIS_AMC/
├─ .env                         # AWS + Bedrock credentials (already provided)
├─ README.md
├─ backend/                     # Python FastAPI service (the agent)
│  ├─ requirements.txt
│  ├─ app/
│  │  ├─ main.py                # FastAPI app + endpoints
│  │  ├─ config.py              # settings from .env
│  │  ├─ aws_clients.py         # boto3 clients (bedrock, sesv2, socialmessaging, dynamodb)
│  │  ├─ router.py              # Lambda-router equivalent (channel normalisation)
│  │  ├─ agent.py               # AgentCore Runtime (Bedrock Converse + tools)
│  │  ├─ tools.py               # AgentCore Gateway tools
│  │  ├─ knowledge_base.py      # KB retrieval (Bedrock KB or local corpus)
│  │  ├─ memory.py              # per-user cross-channel memory
│  │  ├─ guardrails.py          # PII mask, policy, disclaimer
│  │  └─ data/                  # synthetic KB + investor profiles
│  └─ tests/test_smoke.py
├─ frontend/                    # Node.js Express UI
│  ├─ package.json
│  ├─ server.js                 # static server + API proxy
│  └─ public/ (index.html, styles.css, app.js)
└─ docs/
   ├─ ARCHITECTURE.md
   ├─ FLOW.md                   # flow diagrams (mermaid)
   └─ API.md
```

## Prerequisites

- Python 3.10+ (tested on 3.12)
- Node.js 18+ (tested on 24)
- AWS credentials with **Amazon Bedrock** access to the configured model, in a
  Region where the model is available. These are read from the repo-root `.env`.

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

Open **http://localhost:3000**. Use the profile switcher (top right) to try
Ravi (WhatsApp/Hinglish), Priya (Email/English), or Guest (web). Ask things like:

- "Tell me about the Axis ELSS tax saver fund"
- "How do I start a SIP?" / "SIP kaise shuru karein?"
- "Explain the KYC process"
- "I want to talk to a human advisor"

The **Agent activity** panel shows WhatsApp/email messages and advisor hand-offs
the agent produced.

## How it works

The browser talks only to the Node server, which proxies to the Python backend
(so credentials never reach the client). The backend runs an Amazon Bedrock
tool-using agent: it normalises the channel, loads per-user memory, applies
guardrails, retrieves fund docs from the knowledge base, calls gateway tools,
and returns a grounded answer with intent-based CTAs.

See [`docs/FLOW.md`](docs/FLOW.md) for the full flow diagram and
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the component mapping.

## Configuration

Everything works out of the box with the provided `.env`. Optional overrides
(also read from `.env`) let you connect real AWS resources:

| Variable | Default | Purpose |
|----------|---------|---------|
| `AWS_REGION` | `ap-south-1` | Region for all clients |
| `LLM_MODEL` | Claude 3.5 Sonnet (APAC) | Bedrock model id |
| `KNOWLEDGE_BASE_ID` | _(empty)_ | Use a real Bedrock Knowledge Base instead of the local corpus |
| `SIMULATE_CHANNELS` | `true` | `false` to send real WhatsApp/SES messages |
| `WHATSAPP_PHONE_NUMBER_ID` | _(empty)_ | End User Messaging phone id |
| `SES_FROM_EMAIL` | placeholder | Verified SES sender |
| `ADVISOR_ALERT_EMAIL` | placeholder | Hand-off alert recipient |
| `HANDOFF_TABLE` | `mf-advisor-handoff-queue` | DynamoDB table name |

See [`backend/.env.example`](backend/.env.example) for the full list.

## Testing

```powershell
cd backend
.\.venv\Scripts\Activate.ps1
python -m pytest -q            # or: python tests/test_smoke.py
```

The smoke test exercises the knowledge base, guardrails, and the local agent
fallback without requiring network access.

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — design and CDS mapping
- [`docs/FLOW.md`](docs/FLOW.md) — flow diagrams
- [`docs/API.md`](docs/API.md) — endpoint reference
- [`docs/RELEASE_NOTES.md`](docs/RELEASE_NOTES.md) — changelog / release notes
