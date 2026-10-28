# API Reference

Base URL (backend): `http://127.0.0.1:8000`
Interactive docs (FastAPI/Swagger): `http://127.0.0.1:8000/docs`

All endpoints are also reachable through the frontend proxy at
`http://localhost:3000` (same paths), which is what the browser UI uses.

---

## GET /health
Liveness + configuration summary.

```json
{
  "status": "ok",
  "region": "ap-south-1",
  "model": "apac.anthropic.claude-3-5-sonnet-20241022-v2:0",
  "aws_credentials": true,
  "knowledge_base": "local",
  "channels": "live",
  "auth": "cognito"
}
```
`channels`: `live` (SES) or `simulated`. `auth`: `cognito` or `local`.

---

## GET /api/homepage?user_id=<id>
Personalised homepage: greeting, profile, trending queries and goal-based
suggestions.

---

## Authentication

When Cognito is configured (`auth: "cognito"`), email accounts use
password + email-code flows. The seed demo accounts (`+919000000001`,
`investor2@example.com`) use a password-less local fallback. All auth endpoints
normalise the id (emails lowercased; phones to `+91…`).

### POST /api/login
```json
{ "user_id": "you@email.com", "password": "Secret123" }
```
Returns `{ "status": "ok", "profile": { … } }`. `401` for bad credentials /
unconfirmed user; `404` if not found. Password optional for local demo accounts.

### POST /api/register
```json
{ "name": "Demo User", "user_id": "you@email.com", "password": "Secret123",
  "language": "English", "risk_tolerance": "Moderate",
  "investment_goals": ["Tax saving"] }
```
With Cognito → returns `{ "status": "confirmation_required", "user_id": … }` and
emails a 6-digit code. Without Cognito → creates an in-memory profile and returns
`{ "status": "ok", "profile": { … } }`. New accounts start `kyc_status: "Pending"`.

### POST /api/confirm
Confirm a Cognito sign-up with the emailed code, then log in.
```json
{ "user_id": "you@email.com", "code": "123456" }
```

### POST /api/forgot-password
Emails a password-reset code.
```json
{ "user_id": "you@email.com" }
```

### POST /api/reset-password
Set a new password with the emailed code.
```json
{ "user_id": "you@email.com", "code": "123456", "password": "NewPass123" }
```

> **Confidential data note:** auth here uses Cognito (email + password) or a
> synthetic local fallback. In production, identity/KYC is established via an
> **identity/KYC provider** (PAN + Aadhaar, with consent) and account linkage
> via the **fund house transaction APIs**. Raw PAN/Aadhaar are never stored.

---

## POST /api/chat
Website/app chat + search. Core endpoint.

Request:
```json
{
  "message": "What is the lock-in period for the ELSS tax saver fund?",
  "user_id": "+919000000001",
  "channel": "web",
  "page": "KYC"
}
```

Response:
```json
{
  "answer": "… grounded answer …\n\n---\n*disclaimer*",
  "ctas": [
    { "label": "View fund page", "action": "open", "url": "https://www.axismf.com/mutual-funds/..." },
    { "label": "Talk to an advisor", "action": "handoff", "url": "" }
  ],
  "intent": "information",
  "tool_trace": [ { "tool": "search_fund_docs", "input": {…}, "output": {…} } ],
  "channel": "web",
  "user_id": "+919000000001"
}
```

| Field | Notes |
|-------|-------|
| `message` | user query (required) |
| `user_id` | phone (`+91…`) / email / `guest` — drives profile + memory continuity |
| `channel` | `web` \| `whatsapp` \| `email` |
| `page` | optional current-page context (e.g. `KYC`) |

**CTA URLs** are only kept when they are real fund-house links retrieved from
the knowledge base; invented/hallucinated URLs are stripped to in-app actions.
**Deterministic intents** handled without the model: a hand-off request runs
`handoff_to_advisor`; "complete KYC" runs `request_kyc` (queued, status
"In progress" — never auto-verified); "email me this" sends a live SES summary
to a signed-in user's address.

---

## POST /webhooks/whatsapp
Simulates an inbound WhatsApp event (End User Messaging → SNS). Runs the agent
and pushes the reply back via `send_whatsapp`.

```json
{ "from": "+919000000001", "text": "How do I start a SIP?" }
```

## POST /webhooks/email
Simulates an inbound email (SES receipt rule → S3). Runs the agent and replies
via `send_email`.

```json
{ "from": "investor2@example.com", "subject": "KYC", "body": "Explain the KYC process" }
```

---

## GET /api/outbox
Last 50 simulated/real outbound messages and advisor hand-offs. Powers the
"Agent activity" panel in the UI.

## POST /api/reset?user_id=<id>
Clears a user's conversation memory.
