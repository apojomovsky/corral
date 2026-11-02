// Entry module: routing, view mounting, the poll loop.

import {
  TAIL_POLL_MS,
  VERSION_CHECK_MS,
  forgetToken,
  hasTicket,
  isSupportedApi,
  isSupportedDocument,
  loadConfig,
  loadSnapshot,
  loadTail,
  loadToken,
  loadVersion,
  nextDelay,
  resolveAgent,
  saveToken,
} from "./api.js";
import { canonicalJson, stripVolatile } from "./fmt.js";
import {
  hideToken,
  renderConnection,
  renderDetail,
  renderDetailMissing,
  renderOverview,
  renderShell,
  renderStopped,
  renderTail,
  renderTailButtons,
  renderTailFailed,
  renderTailLoading,
  renderTailMissing,
  renderTailPaused,
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
let lastAgentKey = null;
let lastVersion = null;
let lastVersionCheckMs = 0;
let versionCheckForced = false;
let outdated = false;
// The tail poll is its own loop beside the snapshot poll: one timer,
// its own failure count, and operator state that survives polls.
let tailTimer = null;
let tailFailures = 0;
let tailPaused = false;
let tailFollow = true;
let lastTailKey = null;
let tailTarget = null;

function helpFor(mode) {
  if (mode === "hub") {
    return "Paste the reader token for this hub, minted with the hub add-reader verb.";
  }
  return "Paste the API token for this server. It lives in the host token file; mint one with corral serve --new-token.";
}

function routeFromHash() {
  const hash = window.location.hash || "#/";
  if (hash === "#/") {
    return { name: "overview", project: null, notice: null, key: null };
  }
  if (hash.startsWith("#/?")) {
    let project = null;
    try {
      const picked = new URLSearchParams(hash.slice(3)).get("project");
      project = picked === null || picked === "" ? null : picked;
    } catch {
      project = null;
    }
    return { name: "overview", project, notice: null, key: null };
  }
  if (hash === "#/agent" || hash.startsWith("#/agent?")) {
    let key = null;
    try {
      const query = hash.startsWith("#/agent?") ? hash.slice("#/agent?".length) : "";
      const params = new URLSearchParams(query);
      const id = params.get("id");
      if (id !== null && id !== "") {
        key = { kind: "id", id };
      } else {
        const repo = params.get("repo");
        const number = params.get("number");
        if (repo !== null && repo !== "" && number !== null && number !== "") {
          const rawRole = params.get("role");
          key = {
            kind: "ticket",
            repo,
            number,
            role: rawRole === null || rawRole === "" ? null : rawRole,
          };
        }
      }
    } catch {
      key = null;
    }
    if (key !== null) {
      return { name: "agent", project: null, notice: null, key };
    }
  }
  return {
    name: "unknown",
    project: null,
    notice: "Unknown view " + hash + ", showing the overview.",
    key: null,
  };
}

// The row a detail key names, drawn, with the row for the tail poll.
// A null row draws the miss notice; a throw leaves every cache and
// the live page untouched for the caller to fall through.
function drawAgentDetail(doc, key, nowMs) {
  const row = resolveAgent(doc, key);
  if (row === null) {
    renderDetailMissing();
    return null;
  }
  renderDetail(row, { nowMs });
  return row;
}

function renderCurrent(config, token) {
  if (outdated || lastDoc === null) {
    stopTail();
    return false;
  }
  const route = routeFromHash();
  if (route.name === "agent") {
    let row = null;
    try {
      row = drawAgentDetail(lastDoc, route.key, Date.now());
    } catch {
      return false;
    }
    // The cache updates only after a clean draw, so a throw above
    // retries on the next poll instead of skipping.
    lastAgentKey = canonicalJson(route.key);
    lastProject = null;
    lastNotice = null;
    syncTailPoll(config, token, row);
    return true;
  }
  try {
    renderOverview(lastDoc, { project: route.project, nowMs: Date.now(), notice: route.notice });
  } catch {
    return false;
  }
  // The cache updates only after a clean draw, so a throw above
  // retries on the next poll instead of skipping.
  lastProject = route.project;
  lastNotice = route.notice;
  lastAgentKey = null;
  stopTail();
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
  lastAgentKey = null;
  stopTail();
  renderStopped(message);
}

function stopTail() {
  if (tailTimer !== null) {
    window.clearTimeout(tailTimer);
    tailTimer = null;
  }
  lastTailKey = null;
  tailTarget = null;
}

function scheduleTail(config, token, delayMs) {
  if (tailTimer !== null) {
    window.clearTimeout(tailTimer);
    tailTimer = null;
  }
  if (document.hidden || tailPaused || outdated) {
    return;
  }
  tailTimer = window.setTimeout(() => {
    void tickTail(config, token);
  }, delayMs);
}

function tailAtBottom() {
  const pane = document.getElementById("tail");
  return pane.scrollHeight - pane.scrollTop - pane.clientHeight <= 8;
}

// Keeps the tail poll on its ticket: a new ticket clears the pane
// and reloads at once, the same ticket re-arms a dead timer (after
// a visibility return or a resume), anything else stops the loop.
function syncTailPoll(config, token, row) {
  if (row !== null && hasTicket(row)) {
    const target = { repo: String(row.repo), number: String(row.number) };
    const key = target.repo + "\n" + target.number;
    tailTarget = target;
    if (key !== lastTailKey) {
      lastTailKey = key;
      tailFailures = 0;
      renderTailLoading();
    }
    if (tailTimer === null && !tailPaused && !document.hidden && !outdated) {
      void tickTail(config, token);
    }
    return;
  }
  stopTail();
}

async function tickTail(config, token) {
  tailTimer = null;
  if (tailTarget === null || outdated || lastDoc === null) {
    return;
  }
  const target = tailTarget;
  try {
    let reply = null;
    try {
      reply = await loadTail(token, target.repo, target.number);
    } catch {
      reply = null;
    }
    if (target !== tailTarget) {
      return;
    }
    if (reply && reply.status === 200) {
      tailFailures = 0;
      const pinned = tailFollow && tailAtBottom();
      renderTail(reply.text === null ? "" : reply.text);
      if (pinned) {
        const pane = document.getElementById("tail");
        pane.scrollTop = pane.scrollHeight;
      }
    } else if (reply && reply.status === 404) {
      // No session file yet (or any more): an answer, not a failure.
      tailFailures = 0;
      renderTailMissing();
    } else {
      tailFailures += 1;
      renderTailFailed();
    }
  } catch {
    tailFailures += 1;
    renderTailFailed();
  } finally {
    if (target === tailTarget && tailTarget !== null) {
      scheduleTail(config, token, tailFailures === 0 ? TAIL_POLL_MS : nextDelay(tailFailures));
    }
  }
}

function bindTailButtons(config, getToken) {
  const pause = document.getElementById("tail-pause");
  const follow = document.getElementById("tail-follow");
  const pane = document.getElementById("tail");
  renderTailButtons(tailPaused, tailFollow);
  pause.addEventListener("click", () => {
    tailPaused = !tailPaused;
    renderTailButtons(tailPaused, tailFollow);
    if (tailPaused) {
      if (tailTimer !== null) {
        window.clearTimeout(tailTimer);
        tailTimer = null;
      }
      renderTailPaused(true);
    } else {
      renderTailPaused(false);
      void tick(config, getToken());
    }
  });
  follow.addEventListener("click", () => {
    tailFollow = !tailFollow;
    renderTailButtons(tailPaused, tailFollow);
    if (tailFollow) {
      pane.scrollTop = pane.scrollHeight;
    }
  });
  pane.addEventListener("scroll", () => {
    // Scrolling up disengages follow; only the button re-engages it.
    if (tailFollow && !tailAtBottom()) {
      tailFollow = false;
      renderTailButtons(tailPaused, tailFollow);
    }
  });
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
        const route = routeFromHash();
        const fingerprint = canonicalJson(stripVolatile(reply.data));
        const agentKeyText = route.name === "agent" ? canonicalJson(route.key) : null;
        if (
          fingerprint !== lastFingerprint ||
          route.project !== lastProject ||
          route.notice !== lastNotice ||
          agentKeyText !== lastAgentKey
        ) {
          // The cache updates only after a clean draw, so a throw
          // below retries on the next poll instead of skipping.
          if (route.name === "agent") {
            drawAgentDetail(reply.data, route.key, seenMs);
          } else {
            renderOverview(reply.data, {
              project: route.project,
              nowMs: seenMs,
              notice: route.notice,
            });
          }
          lastDoc = reply.data;
          lastFingerprint = fingerprint;
          lastProject = route.project;
          lastNotice = route.notice;
          lastAgentKey = agentKeyText;
        }
        syncTailPoll(
          config,
          token,
          route.name === "agent" ? resolveAgent(reply.data, route.key) : null,
        );
      }
      renderConnection({ live: true, seenMs });
    } else if (reply && reply.status === 401) {
      stopTail();
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
    // never kill the loop: renders swap fully built trees, so the
    // last good data stays in place under its age banner; count the
    // failure and say the draw failed.
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
      // The tail timer chain holds its own token closure: drop it so
      // the tick below re-arms the tail on the pasted token.
      stopTail();
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
  const route = routeFromHash();
  if (route.name === "unknown") {
    renderShell({ mode: config.mode, api: config.api, version: null, seenMs: Date.now() });
  }
  bindToken(config, (value) => {
    token = value;
  });
  bindTailButtons(config, () => token);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      versionCheckForced = true;
      void tick(config, token);
    }
  });
  window.addEventListener("hashchange", () => {
    if (!renderCurrent(config, token)) {
      void tick(config, token);
    }
  });
  await tick(config, token);
}

void boot();
