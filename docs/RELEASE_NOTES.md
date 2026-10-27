# Release Notes — Axis AMC MF Investor Assistant

All notable changes to this project are documented here.
Dates are in YYYY-MM-DD. This project follows a simple, human-readable changelog.

---

## [0.2.0] — 2026-10-27

Feature release building on the initial reference implementation. Focus: real
investor journeys (login, KYC, email), reliable CTAs, and clear production
sourcing of confidential data.

### Added
- **Login & register (demo auth).** Header Login/Logout plus a modal.
  - `POST /api/login` authenticates against synthetic + runtime-registered
    investor profiles.
  - `POST /api/register` creates a new **in-memory** profile and signs in
    (new accounts start with `kyc_status: "Pending"`).
  - User ids are normalised (emails lowercased; phones to `+91` form).
- **"Email me this" in web chat.** A signed-in user can say "email me this" /
  "share this on email" (or click the **Email me this** CTA) and the assistant
  emails the last answer to their address via `send_email` (Amazon SES / sesv2),
  then confirms. Guests are prompted to sign in; phone-only accounts are asked
  for an email.
- **KYC completion.** `complete_kyc` tool + deterministic "Complete KYC" flow
  that actually marks the signed-in profile `Verified` and returns a reference,
  instead of looping. `get_service_status` now reflects the real profile KYC
  status.
- **Deterministic advisor hand-off.** "Talk to an advisor" always queues a
  hand-off (`handoff_to_advisor`) and confirms with a ticket id, independent of
  the model.
- **Flow diagrams as PNGs.** `docs/diagrams/flow-request.png` and
  `flow-intent.png`, embedded in `docs/FLOW.md` (Mermaid source kept in
  collapsible blocks).
- **Confidential-data documentation.** README, `docs/ARCHITECTURE.md` (§5) and
  `docs/FLOW.md` now state that KYC/identity/transaction data is synthetic in
  the demo and, in production, comes from **DigiLocker** (identity/KYC, consent
  based) and **Axis Bank APIs** (SIP/redemption/folio). Raw PAN/Aadhaar are
  never stored by this service. Code carries matching `PRODUCTION NOTE`s.

### Changed
- **CTA links point to the real Axis MF website.** Replaced placeholder
  `docs.axismf.example` URLs with verified `axismf.com` fund pages / documents.
  The backend only allows navigable CTA URLs that were actually retrieved from
  the knowledge base; invented/hallucinated URLs are stripped to in-app actions.
- **Advisor CTA never navigates.** "Talk to an advisor" always triggers the
  in-app hand-off (no more 404 on an invented advisor page).
- **Tool identity is enforced server-side.** Every user-scoped tool call
  (`get_service_status`, `complete_kyc`, `handoff_to_advisor`) uses the real
  authenticated `user_id`, ignoring any id the model supplies (correctness +
  prevents cross-user data access).
- **Removed the "Quick actions" homepage section** (HTML, JS, and the unused
  `quick_actions` field in `/api/homepage`).
- Factsheet delivery no longer generates local PDFs; links go to the real site.

### Fixed
- **Login/register modal would not close** after a successful sign-in. Caused by
  `.modal-overlay { display: flex }` overriding the HTML `hidden` attribute;
  fixed with `.modal-overlay[hidden] { display: none !important; }`.
- **KYC status false negative.** "Check my KYC status" sometimes reported
  "not available" for a verified user because the model passed a malformed
  `user_id` to `get_service_status`; fixed by forcing the authenticated id.
- **"Complete KYC" / "Start Video KYC" infinite loop.** The model promised a
  non-existent video-KYC flow and re-asked endlessly; now a single deterministic
  `complete_kyc` call resolves it.
- **Model JSON parsing.** Tolerates unescaped control characters and unwraps
  answers the model occasionally double-nested, with a local fallback on empty
  output.

### Operational notes
- Confidential data and channels are **synthetic/simulated by default**
  (`SIMULATE_CHANNELS=true`). To send real email/WhatsApp, set
  `SIMULATE_CHANNELS=false` and configure SES/WhatsApp (see `backend/.env.example`).
- In-memory state (registered users, KYC verification) resets on backend
  restart — expected for this demo.
- Port tip: if a server fails to start with a bind/`EADDRINUSE` error, a stale
  process is holding the port — free it first:
  `Get-NetTCPConnection -LocalPort 8000 -State Listen | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }`

---

## [0.1.0] — 2026-10-26

Initial reference implementation of the Mutual Fund Investor Assistant
(agentic WhatsApp + Email + Web self-service on AWS), per the Axis AMC
Search + Chatbot PRD and the CDS reference architecture.

### Added
- **Python FastAPI backend** implementing the AgentCore pattern:
  - AgentCore Runtime (`agent.py`) — Amazon Bedrock Converse tool-use loop
    (Claude 3.5 Sonnet), intent + context handling, English/Hinglish.
  - Lambda Router (`router.py`), AgentCore Gateway tools (`tools.py`),
    AgentCore Memory (`memory.py`), Guardrails (`guardrails.py`).
  - Tools: `search_fund_docs`, `send_whatsapp`, `send_email`,
    `get_service_status`, `handoff_to_advisor`.
  - Knowledge base retrieval (Bedrock Knowledge Base with local-corpus
    fallback) over synthetic Axis MF factsheets, SIDs and FAQs.
- **Node.js (Express) frontend** — unified search bar, AI results + chat
  window, personalised homepage, intent-based CTAs, agent-activity panel;
  proxies all API calls to the backend so credentials never reach the browser.
- **Guardrails** — PII masking (PAN/Aadhaar/account/phone/email), a no
  personalised-advice policy, and a statutory market-risk disclaimer on every
  answer.
- **Graceful degradation** — runs end-to-end locally: local KB fallback,
  simulated channels (OUTBOX), and a deterministic model fallback.
- **Docs & tests** — `README.md`, `docs/ARCHITECTURE.md`, `docs/FLOW.md`,
  `docs/API.md`, and an offline smoke-test suite.
