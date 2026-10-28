/* MF Investor Assistant - frontend logic (vanilla JS, no build step). */

const $ = (sel) => document.querySelector(sel);
const state = { userId: "guest", channel: "web" };

const el = {
  authStatus: $("#auth-status"),
  loginBtn: $("#login-btn"),
  logoutBtn: $("#logout-btn"),
  authModal: $("#auth-modal"),
  modalClose: $("#modal-close"),
  loginForm: $("#login-form"),
  registerForm: $("#register-form"),
  confirmForm: $("#confirm-form"),
  forgotForm: $("#forgot-form"),
  resetForm: $("#reset-form"),
  loginId: $("#login-id"),
  loginPassword: $("#login-password"),
  loginError: $("#login-error"),
  registerError: $("#register-error"),
  confirmError: $("#confirm-error"),
  forgotError: $("#forgot-error"),
  resetError: $("#reset-error"),
  confirmHint: $("#confirm-hint"),
  resetHint: $("#reset-hint"),
  forgotLink: $("#forgot-link"),
  pageContext: $("#page-context"),
  searchForm: $("#search-form"),
  searchInput: $("#search-input"),
  homepage: $("#homepage"),
  greeting: $("#greeting"),
  trending: $("#trending"),
  personalisedCard: $("#personalised-card"),
  personalised: $("#personalised"),
  chatPanel: $("#chat-panel"),
  messages: $("#messages"),
  chatForm: $("#chat-form"),
  chatText: $("#chat-text"),
  outboxList: $("#outbox-list"),
};

function channel() {
  return state.channel || "web";
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

    if ((data.personalised || []).length) {
      el.personalisedCard.hidden = false;
      el.personalised.innerHTML = data.personalised
        .map(
          (p) =>
            `<p>🎯 <strong>${p.goal}</strong>: ${p.fund} — ` +
            `<a href="${p.fund_url || p.pdf_url}" target="_blank" rel="noopener">fund page</a>` +
            ` · <a href="${p.pdf_url}" target="_blank" rel="noopener">SID</a></p>`
        )
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

/* A CTA is a "hand-off" if its action or label asks to reach a human advisor.
   These must trigger the in-app agent, never navigate to an external URL
   (the model sometimes invents a non-existent advisor page). */
function isHandoff(c) {
  const hay = `${c.action || ""} ${c.label || ""}`.toLowerCase();
  return /advisor|handoff|hand-off|human|agent|representative|talk to/.test(hay);
}

/* Treat a URL as navigable only if it is a fund-house domain link. The backend
   already strips invented/hallucinated URLs, so by the time a CTA reaches the
   browser with a non-empty url it is a verified fund page/PDF. */
function isUsableUrl(url) {
  if (!url || url.length < 8) return false;
  return /^https:\/\/([a-z0-9-]+\.)*axismf\.com\//i.test(url);
}

/* Detect auth-related CTAs so they open the login/register modal instead of
   looping a chat message about a login page that doesn't exist. */
function authCtaTab(c) {
  const hay = `${c.action || ""} ${c.label || ""}`.toLowerCase();
  if (/register|sign up|create account|new user/.test(hay)) return "register";
  if (/log ?in|sign in/.test(hay)) return "login";
  return null;
}

function ctaButton(c) {
  const authTab = authCtaTab(c);
  const handoff = !authTab && isHandoff(c);
  const navigable = !authTab && !handoff && isUsableUrl(c.url);
  const node = document.createElement(navigable ? "a" : "button");
  node.className = "cta";
  node.textContent = c.label;
  if (navigable) {
    node.href = c.url;
    node.target = "_blank";
    node.rel = "noopener";
  } else {
    node.type = "button";
    node.addEventListener("click", () => {
      if (authTab) {
        openModal(authTab);
      } else if (handoff) {
        sendMessage("Please connect me with a human advisor.");
      } else {
        sendMessage(c.label);
      }
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
        let line;
        if (m.kind === "handoff") {
          line = `Ticket ${m.ticket_id} — ${m.reason}`;
        } else if (m.kind === "agent_turn") {
          const tools = (m.tools_used || []).join(", ") || "none";
          line = `[${m.channel}] "${m.query}" · intent: ${m.intent} · tools: ${tools}`;
        } else {
          line = `to ${m.to}${m.subject ? " · " + m.subject : ""}`;
        }
        return `<div class="outbox-item"><span class="kind">${m.kind.replace("_", " ")}</span>${sim}<div>${line}</div></div>`;
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

/* ---------- Auth / login modal ---------- */
// Pending signup details, used to jump to the confirm step and auto-login.
const pending = { email: null, password: null };

const PANELS = {
  login: el.loginForm,
  register: el.registerForm,
  confirm: el.confirmForm,
  forgot: el.forgotForm,
  reset: el.resetForm,
};

function openModal(tab = "login") {
  el.authModal.hidden = false;
  switchTab(tab);
}
function closeModal() {
  el.authModal.hidden = true;
  [el.loginError, el.registerError, el.confirmError, el.forgotError, el.resetError].forEach(
    (e) => (e.hidden = true)
  );
}
function switchTab(tab) {
  // The two header tabs only reflect login/register; sub-steps keep register active.
  const headerTab = tab === "login" ? "login" : "register";
  document.querySelectorAll(".modal-tab").forEach((t) =>
    t.classList.toggle("active", t.dataset.tab === headerTab)
  );
  Object.entries(PANELS).forEach(([name, form]) => {
    if (form) form.hidden = name !== tab;
  });
}
function showError(elError, message) {
  elError.textContent = message;
  elError.hidden = false;
}

async function setSession(profile) {
  state.userId = profile.user_id;
  state.channel = profile.channel || "web";
  el.authStatus.textContent = `Signed in as ${profile.name}`;
  el.loginBtn.hidden = true;
  el.logoutBtn.hidden = false;
  el.messages.innerHTML = "";
  el.chatPanel.hidden = true;
  closeModal();
  await loadHomepage();
}

async function doLogout() {
  await api(`/api/reset?user_id=${encodeURIComponent(state.userId)}`, { method: "POST" }).catch(() => {});
  state.userId = "guest";
  state.channel = "web";
  el.authStatus.textContent = "Browsing as Guest";
  el.loginBtn.hidden = false;
  el.logoutBtn.hidden = true;
  el.messages.innerHTML = "";
  el.chatPanel.hidden = true;
  loadHomepage();
}

el.loginBtn.addEventListener("click", () => openModal("login"));
el.logoutBtn.addEventListener("click", doLogout);
el.modalClose.addEventListener("click", closeModal);
el.authModal.addEventListener("click", (e) => {
  if (e.target === el.authModal) closeModal();
});
document.querySelectorAll(".modal-tab").forEach((t) =>
  t.addEventListener("click", () => switchTab(t.dataset.tab))
);

el.forgotLink.addEventListener("click", (e) => {
  e.preventDefault();
  $("#forgot-id").value = el.loginId.value.trim();
  switchTab("forgot");
});

// Login
el.loginForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  el.loginError.hidden = true;
  const user_id = el.loginId.value.trim();
  const password = el.loginPassword.value;
  try {
    const { profile } = await api("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id, password }),
    });
    await setSession(profile);
  } catch (err) {
    showError(el.loginError, err.message);
  }
});

// Register -> either signed in (local) or needs email-code confirmation (Cognito)
el.registerForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  el.registerError.hidden = true;
  const user_id = $("#reg-id").value.trim();
  const password = $("#reg-password").value;
  try {
    const res = await api("/api/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: $("#reg-name").value.trim(),
        user_id,
        password,
        language: $("#reg-language").value,
        risk_tolerance: $("#reg-risk").value,
        investment_goals: [$("#reg-goal").value],
      }),
    });
    if (res.status === "confirmation_required") {
      pending.email = user_id;
      pending.password = password;
      el.confirmHint.textContent = `We emailed a 6-digit code to ${user_id}.`;
      switchTab("confirm");
    } else {
      await setSession(res.profile);
    }
  } catch (err) {
    showError(el.registerError, err.message);
  }
});

// Confirm sign-up code, then auto-login
el.confirmForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  el.confirmError.hidden = true;
  try {
    await api("/api/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: pending.email, code: $("#confirm-code").value.trim() }),
    });
    // Auto-login with the password captured at registration.
    const { profile } = await api("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: pending.email, password: pending.password }),
    });
    await setSession(profile);
  } catch (err) {
    showError(el.confirmError, err.message);
  }
});

// Forgot password -> send reset code
el.forgotForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  el.forgotError.hidden = true;
  const user_id = $("#forgot-id").value.trim();
  try {
    await api("/api/forgot-password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id }),
    });
    pending.email = user_id;
    el.resetHint.textContent = `Enter the code we emailed to ${user_id} and your new password.`;
    switchTab("reset");
  } catch (err) {
    showError(el.forgotError, err.message);
  }
});

// Reset password with code -> auto-login
el.resetForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  el.resetError.hidden = true;
  const password = $("#reset-password").value;
  try {
    await api("/api/reset-password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: pending.email, code: $("#reset-code").value.trim(), password }),
    });
    const { profile } = await api("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: pending.email, password }),
    });
    await setSession(profile);
  } catch (err) {
    showError(el.resetError, err.message);
  }
});

loadHomepage();
loadOutbox();
