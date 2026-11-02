// Chat page: sends each message with a fresh Turnstile token, a W3C traceparent and, when signed
// in, the user's ID token, then streams the reply (server-sent events over fetch). Text is only ever
// inserted with textContent, so a reply can't inject markup or script.
//
// Sign-in is email-link (passwordless) through Google's Identity Platform REST API, with no SDK
// script: the page asks for a link, the email brings the user back here with a one-time code, and
// the page exchanges it for an ID token. Tokens live in sessionStorage, so closing the tab signs out.
"use strict";

const config = window.CHAT_CONFIG;
const form = document.getElementById("chat");
const input = document.getElementById("message");
const sendButton = document.getElementById("send");
const statusLine = document.getElementById("status");
const log = document.getElementById("log");

const ERRORS = {
  turnstile_required: "Completa la verificación antes de enviar.",
  turnstile_failed: "La verificación expiró. Inténtalo de nuevo.",
  turnstile_unavailable: "No pudimos verificar que eres una persona. Inténtalo en un momento.",
  auth_unavailable: "No pudimos verificar tu sesión. Inténtalo en un momento.",
  invalid_email: "Ese correo no es válido.",
  too_many_attempts: "Demasiados intentos. Inténtalo más tarde.",
  sign_in_unavailable: "No pudimos enviar el enlace. Inténtalo en un momento.",
  model_unavailable: "El asistente no está disponible ahora. Inténtalo en un momento.",
  invalid_token: "Tu sesión expiró. Inicia sesión de nuevo o continúa sin sesión.",
  content_blocked:
    "No puedo ayudar con ese tipo de contenido. Puedo ayudarte con tus tarjetas, cuentas, pagos y otros temas del banco.",
};

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

function addMessage(kind, text) {
  const el = document.createElement("div");
  el.className = `msg ${kind}`;
  el.textContent = text;
  log.append(el);
  el.scrollIntoView({ block: "end", behavior: "smooth" });
  return el;
}

function addTraceId(el, traceId) {
  const span = document.createElement("span");
  span.className = "trace";
  span.textContent = `trace ${traceId}`;
  el.append(span);
}

async function send(message) {
  sendButton.disabled = true;
  statusLine.textContent = "";
  const token = await freshTurnstileToken();
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
    };
    const idToken = await currentIdToken();
    if (idToken) headers.Authorization = `Bearer ${idToken}`;
    const response = await fetch(`${config.apiUrl}/v1/chat`, {
      method: "POST",
      headers,
      body: JSON.stringify({ message }),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      if (response.status === 401) signOut(ERRORS.invalid_token);
      throw new Error(ERRORS[body.error] || `Error ${response.status}.`);
    }
    await readStream(response, reply);
  } catch (err) {
    reply.textContent = err instanceof Error ? err.message : String(err);
    reply.classList.add("error");
  } finally {
    addTraceId(reply, trace.traceId);
    sendButton.disabled = false;
  }
}

// Parses the API's server-sent events: `data: {"delta": "..."}`, then `event: done` or
// `event: error` with `data: {"error": "..."}`.
async function readStream(response, reply) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let text = "";
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
      if (event === "message" && payload.delta) {
        text += payload.delta;
        reply.textContent = text;
      }
    }
  }
}

form.addEventListener("submit", (e) => {
  e.preventDefault();
  const message = input.value.trim();
  if (!message) return;
  input.value = "";
  send(message);
});

input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    form.requestSubmit();
  }
});

// Turnstile's script loads with `defer` after this one; render the widget once it's there.
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
