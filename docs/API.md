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
  "channels": "simulated"
}
```

---

## GET /api/homepage?user_id=<id>
Personalised homepage: greeting, profile, trending queries, goal-based
suggestions and quick actions.

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
    { "label": "Download factsheet", "action": "download", "url": "https://…pdf" },
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
{ "from": "priya@example.com", "subject": "KYC", "body": "Explain the KYC process" }
```

---

## GET /api/outbox
Last 50 simulated/real outbound messages and advisor hand-offs. Powers the
"Agent activity" panel in the UI.

## POST /api/reset?user_id=<id>
Clears a user's conversation memory.
