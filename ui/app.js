// Chat page: sends each message with a fresh Turnstile token and a W3C traceparent, then streams
// the reply (server-sent events over fetch). Text is only ever inserted with textContent, so a
// reply can't inject markup or script.
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
  model_unavailable: "El asistente no está disponible ahora. Inténtalo en un momento.",
};

let widgetId = null;

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
  const token = window.turnstile && widgetId !== null ? window.turnstile.getResponse(widgetId) : "";
  if (!token) {
    statusLine.textContent = ERRORS.turnstile_required;
    return;
  }
  addMessage("you", message);
  const reply = addMessage("bot", "…");
  const trace = newTraceparent();
  sendButton.disabled = true;
  statusLine.textContent = "";

  try {
    const response = await fetch(`${config.apiUrl}/v1/chat`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "CF-Turnstile-Response": token,
        traceparent: trace.header,
      },
      body: JSON.stringify({ message }),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(ERRORS[body.error] || `Error ${response.status}.`);
    }
    await readStream(response, reply);
  } catch (err) {
    reply.textContent = err instanceof Error ? err.message : String(err);
    reply.classList.add("error");
  } finally {
    addTraceId(reply, trace.traceId);
    sendButton.disabled = false;
    window.turnstile?.reset(widgetId); // each token works once
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
window.addEventListener("load", () => {
  widgetId = window.turnstile.render("#turnstile", {
    sitekey: config.turnstileSiteKey,
    language: "es",
    action: "chat",
  });
});
