// Chat page: sends each message with the visitor's market, a fresh Turnstile token, a W3C
// traceparent and, when signed in, the user's ID token, then streams the reply (server-sent events
// over fetch) with the policy sections it's based on under it. Text is only ever inserted with
// textContent, so a reply or a source can't inject markup or script.
//
// The market comes from a selector built from the API's own list (GET /v1/markets). There's no
// default: the page asks before the first question, since each market's policy differs (ADR
// 0007), and remembers the choice in localStorage.
//
// Sign-in is email-link (passwordless) through Google's Identity Platform REST API, with no SDK
// script: the page asks for a link, the email brings the user back here with a one-time code, and
// the page exchanges it for an ID token. Tokens live in sessionStorage, so closing the tab signs out.
//
// The conversation's ID (from the API's `session` event) lives only in this page's memory, so a
// reload starts a new conversation, as the empty page suggests. Signing out and changing market
// forget it too: the API would start a new conversation anyway, since a session belongs to
// whoever started it and to one market.
"use strict";

const config = window.CHAT_CONFIG;
const form = document.getElementById("chat");
const input = document.getElementById("message");
const sendButton = document.getElementById("send");
const statusLine = document.getElementById("status");
const log = document.getElementById("log");
const marketSelect = document.getElementById("market");

const ERRORS = {
  turnstile_required: "Completa la verificación antes de enviar.",
  turnstile_failed: "La verificación expiró. Inténtalo de nuevo.",
  turnstile_unavailable: "No pudimos verificar que eres una persona. Inténtalo en un momento.",
  auth_unavailable: "No pudimos verificar tu sesión. Inténtalo en un momento.",
  invalid_email: "Ese correo no es válido.",
  too_many_attempts: "Demasiados intentos. Inténtalo más tarde.",
  sign_in_unavailable: "No pudimos enviar el enlace. Inténtalo en un momento.",
  model_unavailable: "El asistente no está disponible ahora. Inténtalo en un momento.",
  knowledge_unavailable: "No pudimos consultar nuestras políticas ahora. Inténtalo en un momento.",
  guardrails_unavailable:
    "No pudimos proteger tus datos en este momento, así que detuvimos la respuesta. Inténtalo en un momento.",
  unknown_market: "Ese país no está disponible. Elige otro de la lista.",
  market_required: "Elige tu país antes de enviar tu pregunta.",
  markets_unavailable: "No pudimos cargar la lista de países. Recarga la página en un momento.",
  invalid_token: "Tu sesión expiró. Inicia sesión de nuevo o continúa sin sesión.",
  content_blocked:
    "No puedo ayudar con ese tipo de contenido. Puedo ayudarte con tus tarjetas, cuentas, pagos y otros temas del banco.",
};

let conversationId = null; // the API's session ID for this conversation, once it sends one
let widgetId = null;
let pendingToken = null; // { resolve } while a Turnstile challenge runs for the next send
let tokenQueue = Promise.resolve(); // one challenge at a time: chat and sign-in share the widget
const TOKEN_TIMEOUT_MS = 30_000; // then give up with no token, so the button never stays disabled

// Tokens expire after 5 minutes and work once, so each send asks for a fresh one. The widget
// renders in execute mode: nothing runs until here, and the checkbox only appears when
// Cloudflare wants an interaction.
function freshTurnstileToken() {
  if (!window.turnstile || widgetId === null) return Promise.resolve("");
  const next = tokenQueue.then(
    () =>
      new Promise((resolve) => {
        const timer = setTimeout(() => settleToken(""), TOKEN_TIMEOUT_MS);
        pendingToken = {
          resolve: (token) => {
            clearTimeout(timer);
            resolve(token);
          },
        };
        window.turnstile.reset(widgetId);
        window.turnstile.execute(widgetId);
      }),
  );
  tokenQueue = next;
  return next;
}

function settleToken(token) {
  pendingToken?.resolve(token);
  pendingToken = null;
}

// The challenge takes a second or more, and it used to run only after Enviar. Now it starts as
// soon as the visitor focuses or types in the message box, so the token is usually ready by the
// time they send. Each token still works once, so the next one starts when they type again.
const PREFETCH_MAX_AGE_MS = 4 * 60_000; // tokens expire after 5 minutes; older ones are refetched
let prefetched = null; // Promise<{ token, at }> for the next chat send

function prefetchToken() {
  if (prefetched || widgetId === null) return;
  prefetched = freshTurnstileToken().then((token) => ({ token, at: Date.now() }));
}

async function tokenForSend() {
  const ready = prefetched;
  prefetched = null; // single use: whatever happens, the next send gets a new one
  if (ready) {
    const { token, at } = await ready;
    if (token && Date.now() - at < PREFETCH_MAX_AGE_MS) return token;
  }
  return freshTurnstileToken();
}

// --- market ---------------------------------------------------------------------------------------

const MARKET = "chat.market"; // localStorage: the code of the last market chosen

function storedMarket() {
  try {
    return localStorage.getItem(MARKET);
  } catch {
    return null; // storage blocked: the visitor just chooses again
  }
}

async function loadMarkets() {
  let markets;
  try {
    const response = await fetch(`${config.apiUrl}/v1/markets`);
    if (!response.ok) throw new Error(String(response.status));
    ({ markets } = await response.json());
  } catch {
    marketSelect.options[0].textContent = "No disponible";
    statusLine.textContent = ERRORS.markets_unavailable;
    return;
  }
  marketSelect.options[0].textContent = "Elige tu país";
  for (const market of markets) {
    const option = document.createElement("option");
    option.value = market.code;
    option.textContent = market.name;
    marketSelect.append(option);
  }
  const saved = storedMarket();
  if (saved && markets.some((m) => m.code === saved)) marketSelect.value = saved;
  marketSelect.disabled = false;
}

marketSelect.addEventListener("change", () => {
  statusLine.textContent = "";
  conversationId = null; // another market's policy: a new conversation (the API starts one anyway)
  try {
    localStorage.setItem(MARKET, marketSelect.value);
  } catch {
    // storage blocked: the choice still holds for this page
  }
});

// --- sign-in --------------------------------------------------------------------------------------

const SESSION = "chat.session"; // sessionStorage: { idToken, refreshToken, expiresAt, email }
const PENDING_EMAIL = "chat.pendingEmail"; // localStorage: the address a link was sent to
const SIGN_IN_ERRORS = {
  INVALID_EMAIL: "Ese correo no es válido.",
  EXPIRED_OOB_CODE: "El enlace expiró. Pide uno nuevo.",
  INVALID_OOB_CODE: "El enlace ya se usó o no es válido. Pide uno nuevo.",
  TOO_MANY_ATTEMPTS_TRY_LATER: "Demasiados intentos. Inténtalo más tarde.",
};

async function identityToolkit(method, body) {
  const url = `https://identitytoolkit.googleapis.com/v1/accounts:${method}?key=${encodeURIComponent(config.signInApiKey)}`;
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const code = String(data?.error?.message || "").split(" ")[0];
    throw new Error(SIGN_IN_ERRORS[code] || "No pudimos completar el inicio de sesión. Inténtalo de nuevo.");
  }
  return data;
}

function loadSession() {
  try {
    return JSON.parse(sessionStorage.getItem(SESSION) || "null");
  } catch {
    return null;
  }
}

function saveSession(session) {
  sessionStorage.setItem(SESSION, JSON.stringify(session));
}

function signOut(message = "") {
  sessionStorage.removeItem(SESSION);
  conversationId = null; // the next message starts a conversation of the anonymous visitor's own
  renderAccount(message);
}

function renderAccount(message = "") {
  const session = loadSession();
  document.getElementById("sign-in").hidden = Boolean(session);
  document.getElementById("signed-in").hidden = !session;
  document.getElementById("who").textContent = session ? session.email : "";
  document.getElementById("account-status").textContent = message;
}

// The one-time code arrives in this page's URL, directly or inside a `link` parameter.
function signInLinkParams() {
  let params = new URLSearchParams(location.search);
  const nested = params.get("link");
  if (nested) {
    try {
      params = new URL(nested).searchParams;
    } catch {
      return null;
    }
  }
  return params.get("mode") === "signIn" && params.get("oobCode") ? params : null;
}

async function completeSignInFromLink() {
  const params = signInLinkParams();
  if (!params) return;
  history.replaceState(null, "", location.pathname); // the code is single-use; keep it out of history
  const email = localStorage.getItem(PENDING_EMAIL) || window.prompt("Confirma el correo con el que pediste el enlace:");
  if (!email) return;
  try {
    const result = await identityToolkit("signInWithEmailLink", { email, oobCode: params.get("oobCode") });
    saveSession({
      idToken: result.idToken,
      refreshToken: result.refreshToken,
      expiresAt: Date.now() + Number(result.expiresIn) * 1000,
      email: result.email,
    });
    localStorage.removeItem(PENDING_EMAIL);
    renderAccount("Listo: iniciaste sesión.");
  } catch (err) {
    renderAccount(err.message);
  }
}

// A valid ID token for the current session, refreshed a minute before it expires; null when signed out.
async function currentIdToken() {
  const session = loadSession();
  if (!session) return null;
  if (Date.now() < session.expiresAt - 60_000) return session.idToken;
  const response = await fetch(`https://securetoken.googleapis.com/v1/token?key=${encodeURIComponent(config.signInApiKey)}`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ grant_type: "refresh_token", refresh_token: session.refreshToken }),
  });
  if (!response.ok) {
    signOut("Tu sesión expiró. Inicia sesión de nuevo.");
    return null;
  }
  const data = await response.json();
  saveSession({
    ...session,
    idToken: data.id_token,
    refreshToken: data.refresh_token,
    expiresAt: Date.now() + Number(data.expires_in) * 1000,
  });
  return data.id_token;
}

// The API sends the link, after the same bot check as chat, so the page's key can't send email.
async function sendSignInLink(email) {
  const token = await freshTurnstileToken();
  if (!token) throw new Error(ERRORS.turnstile_required);
  const response = await fetch(`${config.apiUrl}/v1/sign-in-link`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "CF-Turnstile-Response": token },
    body: JSON.stringify({ email }),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(ERRORS[body.error] || "No pudimos enviar el enlace. Inténtalo de nuevo.");
  }
}

function setUpSignIn() {
  if (!config.signInApiKey) return; // local runs: no sign-in, anonymous tier
  document.getElementById("account").hidden = false;
  renderAccount();
  document.getElementById("sign-in").addEventListener("submit", async (e) => {
    e.preventDefault();
    const email = document.getElementById("email").value.trim();
    const button = document.getElementById("send-link");
    button.disabled = true;
    try {
      await sendSignInLink(email);
      localStorage.setItem(PENDING_EMAIL, email);
      renderAccount(`Te enviamos un enlace a ${email}. Ábrelo en este navegador.`);
    } catch (err) {
      renderAccount(err.message);
    } finally {
      button.disabled = false;
    }
  });
  document.getElementById("sign-out").addEventListener("click", () => signOut("Cerraste sesión."));
  completeSignInFromLink();
}

// The browser starts the trace, so Cloud Trace links this request from the page to the model.
function newTraceparent() {
  const hex = (bytes) =>
    Array.from(crypto.getRandomValues(new Uint8Array(bytes)), (b) => b.toString(16).padStart(2, "0")).join("");
  const traceId = hex(16);
  return { traceId, header: `00-${traceId}-${hex(8)}-01` };
}

// A message's text lives in its own element, so the sources and the trace ID can follow it.
function addMessage(kind, text) {
  const el = document.createElement("div");
  el.className = `msg ${kind}`;
  const body = document.createElement("div");
  body.className = "text";
  body.textContent = text;
  el.append(body);
  log.append(el);
  el.scrollIntoView({ block: "end", behavior: "smooth" });
  return el;
}

function setText(el, text) {
  el.querySelector(".text").textContent = text;
}

// The policy sections the answer is based on, collapsed so a short answer stays short. The summary
// names the first one, so two markets' answers differ at a glance.
function addSources(el, sources) {
  if (!sources.length) return;
  const details = document.createElement("details");
  details.className = "sources";
  const summary = document.createElement("summary");
  summary.textContent = `Fuentes (${sources.length}): ${sources[0].title}`;
  const list = document.createElement("ul");
  for (const source of sources) {
    const item = document.createElement("li");
    item.textContent = `${source.title} — ${source.section}`;
    list.append(item);
  }
  details.append(summary, list);
  el.append(details);
}

// Where the wait went, under each answer: the bot check in the browser, then the API and the model
// until the first word. The server's own breakdown is in the trace, which also gets the bot-check
// time (X-Client-Timing), so one trace covers the whole wait.
function addTraceId(el, traceId, timing) {
  const seconds = (ms) => `${(ms / 1000).toFixed(1)} s`;
  const parts = [`trace ${traceId}`];
  if (timing.check !== undefined) parts.push(`verificación ${seconds(timing.check)}`);
  if (timing.firstWord !== undefined) parts.push(`primera palabra ${seconds(timing.firstWord)}`);
  const span = document.createElement("span");
  span.className = "trace";
  span.textContent = parts.join(" · ");
  el.append(span);
}

async function send(message) {
  const market = marketSelect.value;
  if (!market) {
    statusLine.textContent = marketSelect.disabled ? ERRORS.markets_unavailable : ERRORS.market_required;
    input.value = message; // keep the question for after the choice
    marketSelect.focus();
    return;
  }
  sendButton.disabled = true;
  statusLine.textContent = "";
  const started = performance.now();
  const token = await tokenForSend();
  const timing = { check: performance.now() - started };
  if (!token) {
    statusLine.textContent = ERRORS.turnstile_required;
    sendButton.disabled = false;
    return;
  }
  addMessage("you", message);
  const reply = addMessage("bot", "…");
  const trace = newTraceparent();

  try {
    const headers = {
      "Content-Type": "application/json",
      "CF-Turnstile-Response": token,
      traceparent: trace.header,
      "X-Client-Timing": `check=${Math.round(timing.check)}`, // the wait the server can't see
    };
    const idToken = await currentIdToken();
    if (idToken) headers.Authorization = `Bearer ${idToken}`;
    const sent = performance.now();
    const response = await fetch(`${config.apiUrl}/v1/chat`, {
      method: "POST",
      headers,
      body: JSON.stringify({ message, market, session_id: conversationId }),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      if (response.status === 401) signOut(ERRORS.invalid_token);
      throw new Error(ERRORS[body.error] || `Error ${response.status}.`);
    }
    const firstWordAt = await readStream(response, reply);
    if (firstWordAt !== null) timing.firstWord = firstWordAt - sent;
  } catch (err) {
    reply.querySelector(".sources")?.remove(); // the answer didn't happen, so neither did its sources
    setText(reply, err instanceof Error ? err.message : String(err));
    reply.classList.add("error");
  } finally {
    addTraceId(reply, trace.traceId, timing);
    sendButton.disabled = false;
  }
}

// Parses the API's server-sent events: `event: session` and `event: sources` first, then
// `data: {"delta": "..."}`, then `event: done` or `event: error` with `data: {"error": "..."}`.
// Returns when the first word arrived.
async function readStream(response, reply) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let text = "";
  let firstWordAt = null;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let end;
    while ((end = buffer.indexOf("\n\n")) !== -1) {
      const block = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      let event = "message";
      let data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event: ")) event = line.slice(7);
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      const payload = data ? JSON.parse(data) : {};
      if (event === "error") throw new Error(ERRORS[payload.error] || "Error.");
      if (event === "session" && payload.session_id) conversationId = payload.session_id;
      if (event === "sources") addSources(reply, payload.sources || []);
      if (event === "message" && payload.delta) {
        firstWordAt ??= performance.now();
        text += payload.delta;
        setText(reply, text);
      }
    }
  }
  return firstWordAt;
}

form.addEventListener("submit", (e) => {
  e.preventDefault();
  const message = input.value.trim();
  if (!message) return;
  input.value = "";
  send(message);
});

input.addEventListener("focus", prefetchToken);
input.addEventListener("input", prefetchToken);

input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    form.requestSubmit();
  }
});

// Turnstile's script loads with `defer` after this one; render the widget once it's there.
loadMarkets();
setUpSignIn();

window.addEventListener("load", () => {
  widgetId = window.turnstile.render("#turnstile", {
    sitekey: config.turnstileSiteKey,
    language: "es",
    action: "chat",
    execution: "execute",
    appearance: "interaction-only",
    callback: settleToken,
    "error-callback": () => settleToken(""),
    "timeout-callback": () => settleToken(""),
  });
});
