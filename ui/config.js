// Local defaults. The deploy job overwrites this file in the uploaded copy with the deployed API's
// URL and the Turnstile widget's public site key.
// "1x00000000000000000000AA" is Cloudflare's test site key that always passes (for local runs,
// where the API's bot check is off).
window.CHAT_CONFIG = {
  apiUrl: "http://localhost:8000",
  turnstileSiteKey: "1x00000000000000000000AA",
};
