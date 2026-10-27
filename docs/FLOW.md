# MF Investor Assistant — Flow Diagram

This document maps the CDS reference architecture to the implementation in this
repository. The numbered steps match the "Request flow" legend in the
architecture diagram (`MF Investor Assistant – CDS Architecture.png`).

## End-to-end request flow

```mermaid
flowchart TD
    subgraph Channels["Investor channels"]
        WA["WhatsApp (English / Hinglish)"]
        EM["Email"]
        WEB["Website / App search + chat"]
    end

    subgraph Ingress["Ingress (CDS qualifying services)"]
        EUMS["AWS End User Messaging Social<br/>WhatsApp inbound -> SNS"]
        SES_IN["Amazon SES<br/>inbound receipt rule -> S3 (MIME)"]
        SNS["Amazon SNS<br/>WhatsApp events"]
    end

    subgraph Backend["Python FastAPI backend"]
        ROUTER["Lambda Router (router.py)<br/>normalise channel + user id"]
        AGENT["AgentCore Runtime (agent.py)<br/>Bedrock Converse + tool use"]
        MEM["AgentCore Memory (memory.py)<br/>per-user context, cross-channel"]
        GUARD["Guardrails (guardrails.py)<br/>PII mask, no-advice policy, disclaimer"]
        subgraph Gateway["AgentCore Gateway tools (tools.py)"]
            T1["search_fund_docs"]
            T2["send_whatsapp"]
            T3["send_email"]
            T4["get_service_status"]
            T5["handoff_to_advisor"]
        end
    end

    subgraph Knowledge["Knowledge & data"]
        KB["Knowledge Bases for Bedrock<br/>(local corpus fallback)"]
        SVCAPI["Service API (synthetic now)<br/>prod: DigiLocker KYC + Axis Bank APIs"]
        DDB["DynamoDB<br/>advisor hand-off queue"]
    end

    subgraph Egress["Egress (CDS qualifying services)"]
        OUT_WA["socialmessaging:SendWhatsAppMessage"]
        OUT_EM["sesv2:SendEmail (factsheet / summary)"]
        ADV["Advisor / ops hand-off alert"]
    end

    WA -->|1| EUMS -->|2| SNS -->|3| ROUTER
    EM -->|1| SES_IN -->|2| ROUTER
    WEB -->|1 POST /api/chat| ROUTER

    ROUTER -->|invoke| AGENT
    AGENT <-->|4 load/save| MEM
    AGENT <-->|4 enforce| GUARD
    AGENT -->|5| Gateway

    T1 --> KB
    T4 --> SVCAPI
    T5 --> DDB
    T2 -->|6| OUT_WA --> WA
    T3 -->|7| OUT_EM --> EM
    T5 -->|8| ADV

    AGENT -->|answer + CTAs| ROUTER --> WEB
```

## Intent handling

```mermaid
flowchart LR
    Q["User query"] --> NLP["NLP intent + context<br/>(foundation model)"]
    NLP -->|information| INFO["search_fund_docs -> grounded answer"]
    NLP -->|service| SVC["get_service_status / FAQ answer"]
    NLP -->|transaction| TXN["CTA: Start SIP / Invest / Redeem"]
    NLP -->|advice| ADV2["Guardrail: no personalised advice<br/>-> handoff_to_advisor"]
    INFO --> CTA["Intent-based CTAs"]
    SVC --> CTA
    TXN --> CTA
    ADV2 --> CTA
```

## Step reference

| # | Architecture step | Where it lives |
|---|-------------------|----------------|
| 1 | Investor messages on WhatsApp / emails / uses web search | `channels` → `main.py` endpoints |
| 2 | End User Messaging publishes to SNS; SES stores email in S3 | `webhooks/*` + `router.normalise_*` |
| 3 | Lambda router normalises message and invokes the agent | `router.route()` |
| 4 | Agent loads Memory context and applies Guardrails | `agent.run_agent()` |
| 5 | Agent retrieves from KB and calls tools via the Gateway | `agent._run_bedrock_loop()` + `tools.py` |
| 6 | Reply on WhatsApp (text, buttons, factsheet PDF) | `tools.send_whatsapp()` |
| 7 | Factsheet / summary emailed via SES | `tools.send_email()` |
| 8 | "Talk to advisor": hand-off queued + SES alert | `tools.handoff_to_advisor()` |

## Confidential data (KYC / identity / transactions)

The Service API node is **synthetic** in this demo (`tools.get_service_status`,
`tools.complete_kyc`). In production, confidential investor details are not
generated or stored by this assistant — they come from authoritative,
consent-based sources:

- **DigiLocker** for identity and KYC (PAN + Aadhaar, address proof); only the
  verified result flows back to the agent — never raw document numbers.
- **Axis Bank / Axis AMC APIs** for SIP, redemption, folio and account state.

See [ARCHITECTURE.md §5](ARCHITECTURE.md#5-confidential-data-sources-kyc-identity-transactions).
