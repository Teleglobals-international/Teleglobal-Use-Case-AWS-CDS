/* Axis MF Investor Assistant - frontend logic (vanilla JS, no build step). */

const $ = (sel) => document.querySelector(sel);
const state = { userId: "guest", channelFor: { guest: "web", "+919000000001": "whatsapp", "priya@example.com": "email" } };

const el = {
  user: $("#user"),
  pageContext: $("#page-context"),
  searchForm: $("#search-form"),
  searchInput: $("#search-input"),
  homepage: $("#homepage"),
  greeting: $("#greeting"),
  trending: $("#trending"),
  quickActions: $("#quick-actions"),
  personalisedCard: $("#personalised-card"),
  personalised: $("#personalised"),
  chatPanel: $("#chat-panel"),
  messages: $("#messages"),
  chatForm: $("#chat-form"),
  chatText: $("#chat-text"),
  outboxList: $("#outbox-list"),
};

function channel() {
  return state.channelFor[state.userId] || "web";
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err.hint || err.detail || `Request failed (${r.status})`);
  }
  return r.json();
}

/* ---------- Homepage ---------- */
async function loadHomepage() {
  try {
    const data = await api(`/api/homepage?user_id=${encodeURIComponent(state.userId)}`);
    el.greeting.textContent = data.greeting;
    el.trending.innerHTML = "";
    (data.trending || []).forEach((t) => el.trending.appendChild(chip(t, () => runSearch(t))));
    el.quickActions.innerHTML = "";
    (data.quick_actions || []).forEach((q) => el.quickActions.appendChild(chip(q, () => runSearch(q))));

    if ((data.personalised || []).length) {
      el.personalisedCard.hidden = false;
      el.personalised.innerHTML = data.personalised
        .map((p) => `<p>🎯 <strong>${p.goal}</strong>: ${p.fund} — <a href="${p.pdf_url}" target="_blank" rel="noopener">factsheet</a></p>`)
        .join("");
    } else {
      el.personalisedCard.hidden = true;
    }
  } catch (e) {
    el.greeting.textContent = "Backend not reachable";
    el.trending.innerHTML = `<span class="typing">${e.message}</span>`;
  }
}

function chip(label, onClick) {
  const b = document.createElement("button");
  b.className = "chip";
  b.textContent = label;
  b.type = "button";
  b.addEventListener("click", onClick);
  return b;
}

/* ---------- Chat / search ---------- */
function showChat() {
  el.chatPanel.hidden = false;
}

function addUserMsg(text) {
  const d = document.createElement("div");
  d.className = "msg user";
  d.textContent = text;
  el.messages.appendChild(d);
  el.messages.scrollTop = el.messages.scrollHeight;
}

function addTyping() {
  const d = document.createElement("div");
  d.className = "msg bot typing";
  d.textContent = "Assistant is thinking…";
  d.dataset.typing = "1";
  el.messages.appendChild(d);
  el.messages.scrollTop = el.messages.scrollHeight;
  return d;
}

function renderBot(result) {
  const d = document.createElement("div");
  d.className = "msg bot";

  const intent = document.createElement("span");
  intent.className = "intent-tag";
  intent.textContent = `intent: ${result.intent || "information"}`;
  d.appendChild(intent);

  // Split off the disclaimer so we can style it.
  let [body, disclaimer] = splitDisclaimer(result.answer || "");
  const bodyEl = document.createElement("div");
  bodyEl.innerHTML = mdLite(body);
  d.appendChild(bodyEl);

  if ((result.ctas || []).length) {
    const ctas = document.createElement("div");
    ctas.className = "ctas";
    result.ctas.forEach((c) => ctas.appendChild(ctaButton(c)));
    d.appendChild(ctas);
  }

  if (disclaimer) {
    const disc = document.createElement("span");
    disc.className = "disclaimer";
    disc.textContent = disclaimer;
    d.appendChild(disc);
  }
  el.messages.appendChild(d);
  el.messages.scrollTop = el.messages.scrollHeight;
}

function ctaButton(c) {
  const hasUrl = c.url && c.url.length > 3;
  const node = document.createElement(hasUrl ? "a" : "button");
  node.className = "cta";
  node.textContent = c.label;
  if (hasUrl) {
    node.href = c.url;
    node.target = "_blank";
    node.rel = "noopener";
  } else {
    node.type = "button";
    node.addEventListener("click", () => {
      const prompt = c.action === "handoff" ? "Please connect me with a human advisor." : c.label;
      sendMessage(prompt);
    });
  }
  return node;
}

function splitDisclaimer(text) {
  const idx = text.indexOf("---");
  if (idx === -1) return [text, ""];
  return [text.slice(0, idx).trim(), text.slice(idx).replace(/[-*]/g, "").trim()];
}

/* Minimal markdown: bold, bullets, line breaks. */
function mdLite(text) {
  return text
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/^[•\-]\s+(.*)$/gm, "• $1")
    .replace(/\n/g, "<br/>");
}

async function sendMessage(text) {
  showChat();
  addUserMsg(text);
  const typing = addTyping();
  try {
    const result = await api("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message: text,
        user_id: state.userId,
        channel: channel(),
        page: el.pageContext.value || null,
      }),
    });
    typing.remove();
    renderBot(result);
    loadOutbox();
  } catch (e) {
    typing.remove();
    const err = document.createElement("div");
    err.className = "msg bot";
    err.textContent = `⚠️ ${e.message}`;
    el.messages.appendChild(err);
  }
}

function runSearch(q) {
  el.searchInput.value = "";
  sendMessage(q);
}

/* ---------- Outbox ---------- */
async function loadOutbox() {
  try {
    const data = await api("/api/outbox");
    el.outboxList.innerHTML = (data.messages || [])
      .slice()
      .reverse()
      .map((m) => {
        const sim = m.simulated ? '<span class="badge-sim">simulated</span>' : "";
        const line =
          m.kind === "handoff"
            ? `Ticket ${m.ticket_id} — ${m.reason}`
            : `to ${m.to}${m.subject ? " · " + m.subject : ""}`;
        return `<div class="outbox-item"><span class="kind">${m.kind}</span>${sim}<div>${line}</div></div>`;
      })
      .join("") || '<p class="typing">No agent activity yet.</p>';
  } catch {
    /* backend outbox optional */
  }
}

/* ---------- Wiring ---------- */
el.searchForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const q = el.searchInput.value.trim();
  if (q) runSearch(q);
});

el.chatForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const q = el.chatText.value.trim();
  if (q) {
    el.chatText.value = "";
    sendMessage(q);
  }
});

el.user.addEventListener("change", async () => {
  state.userId = el.user.value;
  await api(`/api/reset?user_id=${encodeURIComponent(state.userId)}`, { method: "POST" }).catch(() => {});
  el.messages.innerHTML = "";
  el.chatPanel.hidden = true;
  loadHomepage();
});

loadHomepage();
loadOutbox();
