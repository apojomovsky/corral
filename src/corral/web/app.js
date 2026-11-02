// Entry module: routing, view mounting, the poll loop.

import {
  fetchJson,
  forgetToken,
  loadConfig,
  loadToken,
  loadVersion,
  nextDelay,
  saveToken,
} from "./api.js";
import { hideToken, renderConnection, renderShell, showToken } from "./views.js";

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
  let reply = null;
  try {
    reply = await fetchJson("/v1/version", token);
  } catch {
    reply = null;
  }
  if (reply && reply.status === 200 && reply.data) {
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
  } else if (reply && reply.status === 401) {
    showToken({
      message: "This server needs a token.",
      help: helpFor(config.mode),
    });
    if (seenMs !== null) {
      renderConnection({ live: false, seenMs });
    }
  } else {
    // Any other status, a status-0 network failure, or a throw: count
    // the backoff, show stale-with-age when a last good poll exists
    // (or the never-connected state when none does), reschedule below.
    failures += 1;
    renderConnection({ live: false, seenMs });
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
  timer = window.setTimeout(() => {
    void tick(config, token);
  }, nextDelay(failures));
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
    let reply = null;
    try {
      reply = await loadVersion(value);
    } catch {
      reply = null;
    }
    if (reply && reply.status === 200) {
      const kept = saveToken(value);
      setToken(value);
      input.value = "";
      hideToken();
      failures = 0;
      await tick(config, value);
      if (!kept) {
        showToken({
          message:
            "This device cannot keep this token, so it works for this page only. Paste it again after a reload.",
        });
      }
    } else if (reply && reply.status === 401) {
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
  let configReply = null;
  try {
    configReply = await loadConfig();
  } catch {
    configReply = null;
  }
  if (!configReply || configReply.status !== 200 || !configReply.data) {
    // The server is down at first load: say so and retry on the same
    // backoff as polls, instead of stranding the page on "connecting".
    failures += 1;
    renderConnection({ live: false, seenMs });
    timer = window.setTimeout(() => {
      void boot();
    }, nextDelay(failures));
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
