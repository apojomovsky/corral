// Entry module: routing, view mounting, the poll loop.

import { fetchJson, forgetToken, loadConfig, loadToken, loadVersion, saveToken } from "./api.js";
import { hideToken, renderConnection, renderShell, showToken } from "./views.js";

const POLL_MS = 5000;
const BACKOFF_MS = [5000, 15000, 30000, 60000];

let seenMs = null;
let failures = 0;
let timer = null;

function helpFor(mode) {
  if (mode === "hub") {
    return "Paste the reader token for this hub, minted with the hub add-reader verb.";
  }
  return "Paste the API token for this server. It lives in the host token file; mint one with corral serve --new-token.";
}

function currentRoute() {
  const hash = window.location.hash || "#/";
  if (hash === "#/" || hash.startsWith("#/?")) {
    return { name: "overview" };
  }
  return { name: "unknown", hash };
}

async function tick(config, token) {
  const reply = await fetchJson("/v1/version", token);
  if (reply.status === 200 && reply.data) {
    seenMs = Date.now();
    failures = 0;
    hideToken();
    renderShell({
      mode: config.mode,
      api: config.api,
      version: reply.data,
      seenMs,
      serverNowMs: typeof reply.data.now === "number" ? reply.data.now * 1000 : null,
    });
    renderConnection({ live: true, seenMs });
  } else if (reply.status === 401) {
    showToken({
      message: "This server needs a token.",
      help: helpFor(config.mode),
    });
    if (seenMs !== null) {
      renderConnection({ live: false, seenMs });
    }
  } else {
    failures += 1;
    if (seenMs !== null) {
      renderConnection({ live: false, seenMs });
    }
  }
  schedule(config, token);
}

function schedule(config, token) {
  if (timer !== null) {
    window.clearTimeout(timer);
  }
  if (document.hidden) {
    return;
  }
  const wait = failures === 0 ? POLL_MS : BACKOFF_MS[Math.min(failures - 1, BACKOFF_MS.length - 1)];
  timer = window.setTimeout(() => {
    void tick(config, token);
  }, wait);
}

function bindToken(config, setToken) {
  const save = document.getElementById("token-save");
  const forget = document.getElementById("token-forget");
  const input = document.getElementById("token-input");
  save.addEventListener("click", async () => {
    const value = input.value.trim();
    if (!value) {
      showToken({ message: "Paste a token first." });
      return;
    }
    const reply = await loadVersion(value);
    if (reply.status === 200) {
      saveToken(value);
      setToken(value);
      input.value = "";
      hideToken();
      failures = 0;
      await tick(config, value);
    } else if (reply.status === 401) {
      showToken({ message: "Wrong token: this server answered 401." });
    } else {
      showToken({ message: "Cannot reach the API. Showing nothing yet." });
    }
  });
  forget.addEventListener("click", () => {
    forgetToken();
    setToken(null);
    input.value = "";
    window.location.reload();
  });
  document.getElementById("token-btn").addEventListener("click", () => {
    const panel = document.getElementById("token");
    panel.hidden = !panel.hidden;
  });
}

async function boot() {
  const configReply = await loadConfig();
  if (configReply.status !== 200 || !configReply.data) {
    renderConnection({ live: false, seenMs: Date.now() });
    return;
  }
  const config = configReply.data;
  let token = loadToken();
  const route = currentRoute();
  if (route.name === "unknown") {
    renderShell({ mode: config.mode, api: config.api, version: null, seenMs: Date.now() });
  }
  bindToken(config, (value) => {
    token = value;
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      void tick(config, token);
    }
  });
  window.addEventListener("hashchange", () => {
    void tick(config, token);
  });
  await tick(config, token);
}

void boot();
