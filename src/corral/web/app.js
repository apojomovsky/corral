// Entry module: routing, view mounting, the poll loop.

import {
  VERSION_CHECK_MS,
  forgetToken,
  isSupportedApi,
  isSupportedDocument,
  loadConfig,
  loadSnapshot,
  loadToken,
  loadVersion,
  nextDelay,
  saveToken,
} from "./api.js";
import { canonicalJson, stripVolatile } from "./fmt.js";
import {
  hideToken,
  renderConnection,
  renderOverview,
  renderShell,
  renderStopped,
  showToken,
} from "./views.js";

// The document schema this shell renders. Newer documents proceed
// (additive); older or missing ones render as unsupported. Pinned
// by the suite against the server's own schema number.
const EXPECTED_SCHEMA_VERSION = 2;

let seenMs = null;
let failures = 0;
let timer = null;
let lastDoc = null;
let lastFingerprint = null;
let lastProject = null;
let lastNotice = null;
let lastVersion = null;
let lastVersionCheckMs = 0;
let versionCheckForced = false;
let outdated = false;

function helpFor(mode) {
  if (mode === "hub") {
    return "Paste the reader token for this hub, minted with the hub add-reader verb.";
  }
  return "Paste the API token for this server. It lives in the host token file; mint one with corral serve --new-token.";
}

function projectFromHash() {
  const hash = window.location.hash || "#/";
  if (hash === "#/") {
    return { name: "overview", project: null, notice: null };
  }
  if (hash.startsWith("#/?")) {
    let project = null;
    try {
      const picked = new URLSearchParams(hash.slice(3)).get("project");
      project = picked === null || picked === "" ? null : picked;
    } catch {
      project = null;
    }
    return { name: "overview", project, notice: null };
  }
  return {
    name: "unknown",
    project: null,
    notice: "Unknown view " + hash + ", showing the overview.",
  };
}

function renderCurrent() {
  if (outdated || lastDoc === null) {
    return false;
  }
  const route = projectFromHash();
  lastProject = route.project;
  lastNotice = route.notice;
  renderOverview(lastDoc, { project: route.project, nowMs: Date.now(), notice: route.notice });
  return true;
}

async function checkVersion(config, token, nowMs) {
  if (!versionCheckForced && lastVersion !== null && nowMs - lastVersionCheckMs < VERSION_CHECK_MS) {
    return true;
  }
  versionCheckForced = false;
  let reply = null;
  try {
    reply = await loadVersion(token);
  } catch {
    reply = null;
  }
  if (reply && reply.status === 200 && reply.data) {
    lastVersion = reply.data;
    lastVersionCheckMs = nowMs;
    if (!isSupportedApi(reply.data, config.api)) {
      outdated = true;
      enterStopped("This dashboard is out of date for this server, reload after upgrading.");
      renderConnection({ live: true, seenMs: nowMs });
      return false;
    }
    // A recheck after an upgrade resumes rendering on the next poll.
    outdated = false;
    return true;
  }
  // Any other outcome leaves the last version (or none) in place;
  // the snapshot poll below decides the failure branch.
  return true;
}

// Entering the stopped gate drops the compare cache, so the
// compare-and-skip path cannot skip the redraw when the version
// matches again and an empty page claims to be live.
function enterStopped(message) {
  lastDoc = null;
  lastFingerprint = null;
  renderStopped(message);
}

async function tick(config, token) {
  const nowMs = Date.now();
  try {
    let versionOk = true;
    try {
      versionOk = await checkVersion(config, token, nowMs);
    } catch {
      versionOk = true;
    }
    if (!versionOk) {
      return;
    }
    let reply = null;
    try {
      reply = await loadSnapshot(token);
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
        version: lastVersion,
        seenMs,
        serverNowMs:
          typeof reply.data.now === "number"
            ? reply.data.now * 1000
            : typeof reply.data.fleet_header === "object" &&
                reply.data.fleet_header !== null &&
                typeof reply.data.fleet_header.now === "number"
              ? reply.data.fleet_header.now * 1000
              : null,
      });
      if (!isSupportedDocument(reply.data, EXPECTED_SCHEMA_VERSION)) {
        const value =
          reply.data && typeof reply.data.schema_version === "number"
            ? String(reply.data.schema_version)
            : "missing";
        enterStopped(
          "Unsupported document schema " + value + ": reload after upgrading.",
        );
      } else {
        const route = projectFromHash();
        const fingerprint = canonicalJson(stripVolatile(reply.data));
        if (
          fingerprint !== lastFingerprint ||
          route.project !== lastProject ||
          route.notice !== lastNotice
        ) {
          // The cache updates only after a clean draw, so a throw
          // below retries on the next poll instead of skipping.
          renderOverview(reply.data, { project: route.project, nowMs: seenMs, notice: route.notice });
          lastDoc = reply.data;
          lastFingerprint = fingerprint;
          lastProject = route.project;
          lastNotice = route.notice;
        }
      }
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
      // Any other status, a status-0 network failure, or a throw:
      // count the backoff, show stale-with-age when a last good poll
      // exists (or the never-connected state when none does).
      failures += 1;
      renderConnection({ live: false, seenMs });
    }
  } catch {
    // A render throw (a new server shape, an unexpected null) must
    // never kill the loop: count it, keep the last good data under
    // its age banner, and say the draw failed.
    failures += 1;
    renderConnection({ live: false, seenMs });
    const note = document.getElementById("notice");
    note.hidden = false;
    note.textContent = "";
    note.appendChild(document.createTextNode("Could not draw this update; the next poll retries."));
  } finally {
    schedule(config, token);
  }
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
    // A private-mode browser can refuse the remove: reloading then
    // would resurrect the token through loadToken, so a failed
    // forget says so on screen instead of reloading.
    if (forgetToken()) {
      setToken(null);
      input.value = "";
      window.location.reload();
    } else {
      showToken({
        message:
          "This device cannot forget this token, so it stays saved here. Clear this site's storage by hand to remove it.",
      });
    }
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
  const route = projectFromHash();
  if (route.name === "unknown") {
    renderShell({ mode: config.mode, api: config.api, version: null, seenMs: Date.now() });
  }
  bindToken(config, (value) => {
    token = value;
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      versionCheckForced = true;
      void tick(config, token);
    }
  });
  window.addEventListener("hashchange", () => {
    if (!renderCurrent()) {
      void tick(config, token);
    }
  });
  await tick(config, token);
}

void boot();
