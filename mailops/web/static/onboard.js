// Mailops · Mobile onboarding client
//
// Security contract (mirrors mailops/web/app.py):
//  * The server-side session cookie (`mailops_sid`) is set/cleared by the
//    server; this script never reads it. It carries the CSRF token in a
//    per-session JS variable and sends it as a request header.
//  * The password input is never written to localStorage, sessionStorage,
//    history or any URL. It is cleared from the DOM the moment the
//    validate request is sent (regardless of success).
//  * All requests are same-origin JSON over POST (CSRF-protected). GETs
//    only fetch non-secret state.
//  * The Telegram Mini App SDK provides `initData`, which the server
//    verifies with HMAC-SHA256 keyed by a hash of the bot token.
"use strict";

const HOSTINGER_DEFAULTS = {
  imap_host: "imap.hostinger.com",
  imap_port: 993,
  smtp_host: "smtp.hostinger.com",
  smtp_port: 465,
};

const CSRF_HEADER = "X-Mailops-CSRF";

let CSRF = null;
let TG_USER = null;

const $ = (sel) => document.querySelector(sel);
const show = (sel) => { $(sel).hidden = false; };
const hide = (sel) => { $(sel).hidden = true; };

function setStatus(msg, kind = "") {
  const el = $("#status");
  el.textContent = msg || "";
  el.className = "status" + (kind ? " " + kind : "");
}

async function api(path, { method = "POST", body = null, auth = true } = {}) {
  const headers = { "Content-Type": "application/json" };
  if (auth && CSRF) headers[CSRF_HEADER] = CSRF;
  const resp = await fetch(path, {
    method,
    credentials: "same-origin",
    headers,
    body: body == null ? undefined : JSON.stringify(body),
  });
  let data = {};
  try { data = await resp.json(); } catch (_e) { /* non-JSON error */ }
  if (!resp.ok) {
    const err = new Error(data.error || resp.statusText || "request failed");
    err.status = resp.status;
    err.body = data;
    throw err;
  }
  return data;
}

function initData() {
  const tg = window.Telegram && window.Telegram.WebApp;
  if (!tg) throw new Error("Telegram Mini App SDK not available");
  return tg.initData || "";
}

async function startSession() {
  setStatus("Verifying your Telegram login…");
  const data = initData();
  if (!data) throw new Error("No initData — open this page from Telegram.");
  const resp = await api("/api/session", { body: { init_data: data }, auth: false });
  CSRF = resp.csrf;
  TG_USER = resp.user;
  const name = (TG_USER.first_name || TG_USER.username || ("id " + TG_USER.id));
  const who = $("#who");
  who.textContent = "Signed in as " + name;
  who.hidden = false;
  hide("#step-auth");
  show("#step-fields");
  setStatus("");
  applyProvider();
}

function applyProvider() {
  const form = $("#step-fields");
  const choice = form.provider.value;
  if (choice === "hostinger") {
    for (const [k, v] of Object.entries(HOSTINGER_DEFAULTS)) {
      const el = form.elements[k];
      if (el && !el.value) el.value = v;
    }
  }
}

function fieldsFromForm() {
  const f = $("#step-fields");
  const data = {};
  for (const key of ["account_id", "display_name", "email",
                     "imap_host", "imap_port", "imap_user",
                     "smtp_host", "smtp_port", "smtp_user"]) {
    const el = f.elements[key];
    if (!el) continue;
    const value = (el.value || "").trim();
    if (!value) continue;
    if (key.endsWith("_port")) {
      const n = parseInt(value, 10);
      if (Number.isNaN(n)) throw new Error(key + " must be a number");
      data[key] = n;
    } else {
      data[key] = value;
    }
  }
  return data;
}

async function submitFields(ev) {
  ev.preventDefault();
  setStatus("Saving…");
  try {
    const patch = fieldsFromForm();
    const resp = await api("/api/fields", { body: patch });
    // Patch returns the merged, server-coerced field set.
    const need = ["account_id", "email", "imap_host", "imap_port", "smtp_host", "smtp_port"];
    const missing = need.filter((k) => !resp.fields[k]);
    if (missing.length) {
      setStatus("Missing: " + missing.join(", "), "err");
      return;
    }
    hide("#step-fields");
    show("#step-password");
    setStatus("");
  } catch (e) {
    setStatus(e.message || "could not save fields", "err");
  }
}

function wirePasswordUI() {
  $("#pw-split").addEventListener("change", (e) => {
    $("#pw-smtp-wrap").hidden = !e.target.checked;
    if (!e.target.checked) $("#pw-smtp").value = "";
  });
  $("#back-to-fields").addEventListener("click", () => {
    $("#pw-main").value = "";
    $("#pw-smtp").value = "";
    hide("#step-password");
    show("#step-fields");
    setStatus("");
  });
}

async function submitValidate(ev) {
  ev.preventDefault();
  const pwMain = $("#pw-main").value;
  const pwSmtp = $("#pw-split").checked ? $("#pw-smtp").value : pwMain;
  if (!pwMain) { setStatus("Enter the mailbox password.", "err"); return; }
  // Scrub inputs immediately so a backgrounded tab doesn't leak the string.
  $("#pw-main").value = "";
  $("#pw-smtp").value = "";
  setStatus("Validating connection…");
  $("#run-validate").disabled = true;
  try {
    const resp = await api("/api/validate", {
      body: { imap_password: pwMain, smtp_password: pwSmtp },
    });
    hide("#step-password");
    show("#step-result");
    renderResult(resp);
  } catch (e) {
    setStatus(e.body && e.body.detail
              ? "Validation failed: " + e.body.detail
              : (e.message || "validation failed"), "err");
  } finally {
    $("#run-validate").disabled = false;
  }
}

function renderResult(r) {
  const list = $("#result-list");
  list.innerHTML = "";
  const row = (label, ok) => {
    const li = document.createElement("li");
    li.textContent = label + ": ";
    const b = document.createElement("span");
    b.className = ok ? "badge-ok" : "badge-fail";
    b.textContent = ok ? "OK" : "FAIL";
    li.appendChild(b);
    list.appendChild(li);
  };
  row("IMAP", r.imap_ok);
  row("SMTP", r.smtp_ok);
  $("#result-detail").textContent = r.detail || "";
  $("#activate").disabled = !r.ok;
  if (r.ok) setStatus("Login handshake succeeded. Review and activate.", "ok");
  else setStatus("Validation failed and was rolled back. Nothing registered.", "err");
}

async function activate() {
  $("#activate").disabled = true;
  setStatus("Activating…");
  try {
    const resp = await api("/api/activate", { body: {} });
    hide("#step-result");
    show("#step-done");
    setStatus("Mailbox '" + resp.account_id + "' activated.", "ok");
  } catch (e) {
    setStatus(e.message || "activation failed", "err");
  }
}

async function cancelAll() {
  try {
    await api("/api/cancel", { body: {} });
  } catch (_e) { /* ignore */ }
  CSRF = null;
  hide("#step-result");
  hide("#step-password");
  hide("#step-fields");
  show("#step-done");
  setStatus("Cancelled. No mailbox was registered.");
}

function bootstrap() {
  wirePasswordUI();
  $("#step-fields").addEventListener("submit", submitFields);
  $("#step-fields").addEventListener("change", (ev) => {
    if (ev.target && ev.target.name === "provider") applyProvider();
  });
  $("#step-password").addEventListener("submit", submitValidate);
  $("#activate").addEventListener("click", activate);
  $("#cancel-all").addEventListener("click", cancelAll);
  startSession().catch((e) => setStatus(e.message || "could not start session", "err"));
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", bootstrap);
} else {
  bootstrap();
}
