/**
 * Axis AMC - MF Investor Assistant frontend (Node.js + Express).
 *
 * Serves the single-page search + chat UI (PRD: unified search bar, AI results
 * page, chat window, personalised homepage, intent-based CTAs) and proxies API
 * calls to the Python FastAPI backend so the browser never needs the backend
 * URL or any credentials.
 */
import express from "express";
import { fileURLToPath } from "url";
import { dirname, join } from "path";

const __dirname = dirname(fileURLToPath(import.meta.url));

const PORT = process.env.PORT || 3000;
const BACKEND_URL = process.env.BACKEND_URL || "http://127.0.0.1:8000";

const app = express();
app.use(express.json());
app.use(express.static(join(__dirname, "public")));

// Node 18+ has global fetch; fall back to node-fetch for older runtimes.
const doFetch = globalThis.fetch
  ? globalThis.fetch
  : (await import("node-fetch")).default;

/** Thin proxy to the backend, forwarding method/body and surfacing errors. */
async function proxy(req, res, path, method = "GET") {
  try {
    const opts = { method, headers: { "Content-Type": "application/json" } };
    if (method !== "GET") opts.body = JSON.stringify(req.body || {});
    const url = new URL(path, BACKEND_URL);
    if (method === "GET" && req.query) {
      Object.entries(req.query).forEach(([k, v]) => url.searchParams.set(k, v));
    }
    const r = await doFetch(url, opts);
    const data = await r.json();
    res.status(r.status).json(data);
  } catch (err) {
    res.status(502).json({
      error: "backend_unreachable",
      detail: String(err),
      hint: `Is the Python backend running at ${BACKEND_URL}?`,
    });
  }
}

app.get("/api/homepage", (req, res) => proxy(req, res, "/api/homepage"));
app.post("/api/chat", (req, res) => proxy(req, res, "/api/chat", "POST"));
app.get("/api/outbox", (req, res) => proxy(req, res, "/api/outbox"));
app.post("/api/reset", (req, res) => proxy(req, res, "/api/reset", "POST"));
app.get("/api/health", (req, res) => proxy(req, res, "/health"));

app.get("*", (_req, res) => res.sendFile(join(__dirname, "public", "index.html")));

app.listen(PORT, () => {
  console.log(`\nAxis MF Assistant frontend running:  http://localhost:${PORT}`);
  console.log(`Proxying API to backend:             ${BACKEND_URL}\n`);
});
