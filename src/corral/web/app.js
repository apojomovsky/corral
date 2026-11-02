// Entry module: routing, view mounting, the poll loop.

import {
  VERSION_CHECK_MS,
  capEvents,
  capJournal,
  forgetToken,
  isSupportedApi,
  isSupportedDocument,
  loadConfig,
  loadEvents,
  loadJournalLast,
  loadJournalOlder,
  loadProjects,
  loadSnapshot,
  loadToken,
  loadVersion,
  nextDelay,
  nextLogDelay,
  olderStart,
  projectNames,
  saveToken,
  snapshotProjects,
} from "./api.js";
import { canonicalJson, stripVolatile } from "./fmt.js";
import {
  hideToken,
  renderConnection,
  renderLog,
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

// Log view memory, one project at a time. The journal accumulates
// oldest-first from the newest window backwards; polls replace the
// newest window in place instead of growing the DOM, and replace
// the whole accumulation when new entries arrive.
let logKind = null;
let logKey = null;
let logJournal = [];
let logJournalStart = 0;
let logJournalTotal = 0;
let logHistory = false;
let logFp = null;
let lastLogView = null;

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
  if (hash === "#/log") {
    return { name: "log", project: null, notice: null };
  }
  if (hash.startsWith("#/log?")) {
    let project = null;
    try {
      const picked = new URLSearchParams(hash.slice(6)).get("project");
      project = picked === null || picked === "" ? null : picked;
    } catch {
      project = null;
    }
    return { name: "log", project, notice: null };
  }
  return {
    name: "unknown",
    project: null,
    notice: "Unknown view " + hash + ", showing the overview.",
  };
}

function logViewKey(route) {
  return String(route.project || "") + "\n" + String(logKind || "");
}

function renderCurrent() {
  const route = projectFromHash();
  if (route.name === "log") {
    if (outdated || lastDoc === null || lastLogView === null || lastLogView.key !== logViewKey(route)) {
      return false;
    }
    lastProject = route.project;
    lastNotice = route.notice;
    renderLog(lastLogView.view, { nowMs: Date.now(), notice: route.notice });
    return true;
  }
  if (outdated || lastDoc === null) {
    return false;
  }
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
  lastLogView = null;
  logFp = null;
  renderStopped(message);
}

function shellFor(config, doc) {
  return {
    mode: config.mode,
    api: config.api,
    version: lastVersion,
    seenMs,
    serverNowMs:
      typeof doc.now === "number"
        ? doc.now * 1000
        : typeof doc.fleet_header === "object" &&
            doc.fleet_header !== null &&
            typeof doc.fleet_header.now === "number"
          ? doc.fleet_header.now * 1000
          : null,
  };
}

// The log view behind one hash route: the project list unions the
// registered names with the set derived from activity; per-card
// failures read as card text with a live connection, since the
// snapshot heartbeat is the freshness signal, not each stack.
async function tickLog(config, token, route) {
  let snapReply = null;
  try {
    snapReply = await loadSnapshot(token);
  } catch {
    snapReply = null;
  }
  if (snapReply && snapReply.status === 401) {
    showToken({
      message: "This server needs a token.",
      help: helpFor(config.mode),
    });
    if (seenMs !== null) {
      renderConnection({ live: false, seenMs });
    }
    return;
  }
  if (!(snapReply && snapReply.status === 200 && snapReply.data)) {
    // Any other status, a status-0 network failure, or a throw:
    // count the backoff, show stale-with-age when a last good poll
    // exists (or the never-connected state when none does).
    failures += 1;
    renderConnection({ live: false, seenMs });
    return;
  }
  seenMs = Date.now();
  failures = 0;
  hideToken();
  renderShell(shellFor(config, snapReply.data));
  if (!isSupportedDocument(snapReply.data, EXPECTED_SCHEMA_VERSION)) {
    const value =
      snapReply.data && typeof snapReply.data.schema_version === "number"
        ? String(snapReply.data.schema_version)
        : "missing";
    enterStopped(
      "Unsupported document schema " + value + ": reload after upgrading.",
    );
    return;
  }
  lastDoc = snapReply.data;
  let regReply = null;
  try {
    regReply = await loadProjects(token);
  } catch {
    regReply = null;
  }
  let registered = null;
  let derivedOnly = false;
  if (
    regReply &&
    regReply.status === 200 &&
    regReply.data &&
    Array.isArray(regReply.data.projects)
  ) {
    registered = regReply.data.projects;
  } else {
    derivedOnly = true;
  }
  const choices = projectNames(registered, snapshotProjects(snapReply.data));
  const key = logViewKey(route);
  if (key !== logKey) {
    logKey = key;
    logJournal = [];
    logJournalStart = 0;
    logJournalTotal = 0;
    logHistory = false;
    logFp = null;
  }
  if (route.project === null) {
    const view = {
      project: null,
      choices,
      derivedOnly,
      unknown: false,
      eventsState: "missing",
      events: [],
      eventsTotal: 0,
      journalState: "missing",
      journal: [],
      journalTotal: 0,
      journalStart: 0,
      kind: logKind,
      history: false,
    };
    lastLogView = { key, view };
    lastProject = route.project;
    lastNotice = route.notice;
    renderLog(view, { nowMs: seenMs, notice: route.notice });
    renderConnection({ live: true, seenMs });
    return;
  }
  let eventsReply = null;
  try {
    eventsReply = await loadEvents(route.project, token);
  } catch {
    eventsReply = null;
  }
  // The kind comes from the route's own tuple and the paging is
  // computed, so a 400 from either stack reads as a bad project
  // name: the project may have been removed since the list loaded.
  let journalReply = null;
  if (!logHistory) {
    try {
      journalReply = await loadJournalLast(route.project, logKind, token);
    } catch {
      journalReply = null;
    }
  }
  if (
    (eventsReply && eventsReply.status === 400) ||
    (journalReply && journalReply.status === 400)
  ) {
    const view = {
      project: route.project,
      choices,
      derivedOnly,
      unknown: true,
      eventsState: "unknown",
      events: [],
      eventsTotal: 0,
      journalState: "unknown",
      journal: [],
      journalTotal: 0,
      journalStart: 0,
      kind: logKind,
      history: false,
    };
    lastLogView = { key, view };
    lastProject = route.project;
    lastNotice = route.notice;
    renderLog(view, { nowMs: seenMs, notice: route.notice });
    renderConnection({ live: true, seenMs });
    return;
  }
  let eventsState = "ready";
  let events = [];
  let eventsTotal = 0;
  if (eventsReply && eventsReply.status === 200 && eventsReply.data) {
    const rows = Array.isArray(eventsReply.data.events) ? eventsReply.data.events : [];
    eventsTotal = rows.length;
    events = capEvents(rows);
  } else {
    eventsState = "failed";
  }
  let journalState = "ready";
  if (journalReply && journalReply.status === 200 && journalReply.data) {
    const entries = Array.isArray(journalReply.data.entries) ? journalReply.data.entries : [];
    logJournalTotal =
      typeof journalReply.data.total === "number" ? journalReply.data.total : entries.length;
    logJournal = entries;
    logJournalStart = Math.max(0, logJournalTotal - entries.length);
  } else if (!logHistory) {
    journalState = "failed";
  }
  const view = {
    project: route.project,
    choices,
    derivedOnly,
    unknown: false,
    eventsState,
    events,
    eventsTotal,
    journalState,
    journal: logJournal,
    journalTotal: logJournalTotal,
    journalStart: logJournalStart,
    kind: logKind,
    history: logHistory,
  };
  const fingerprint =
    canonicalJson(stripVolatile(eventsReply ? eventsReply.data : null)) +
    "|" +
    canonicalJson(choices) +
    "|" +
    (logHistory ? "history:" + canonicalJson(logJournal) : canonicalJson(stripVolatile(logJournal)));
  if (
    fingerprint !== logFp ||
    route.project !== lastProject ||
    route.notice !== lastNotice
  ) {
    lastLogView = { key, view };
    renderLog(view, { nowMs: seenMs, notice: route.notice });
    logFp = fingerprint;
    lastProject = route.project;
    lastNotice = route.notice;
  }
  renderConnection({ live: true, seenMs });
}

async function tickOverview(config, token, route) {
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
    renderShell(shellFor(config, reply.data));
    if (!isSupportedDocument(reply.data, EXPECTED_SCHEMA_VERSION)) {
      const value =
        reply.data && typeof reply.data.schema_version === "number"
          ? String(reply.data.schema_version)
          : "missing";
      enterStopped(
        "Unsupported document schema " + value + ": reload after upgrading.",
      );
    } else {
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
    const route = projectFromHash();
    if (route.name === "log") {
      await tickLog(config, token, route);
    } else if (route.name === "unknown") {
      await tickOverview(config, token, { name: "overview", project: null, notice: route.notice });
    } else {
      await tickOverview(config, token, route);
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
  const route = projectFromHash();
  const delay = route.name === "log" ? nextLogDelay(failures) : nextDelay(failures);
  timer = window.setTimeout(() => {
    void tick(config, token);
  }, delay);
}

// The kind chips and paging buttons are re-created on every draw,
// so one delegated listener on the view outlives them all.
function bindLogView(getToken, pollNow) {
  document.getElementById("view").addEventListener("click", (event) => {
    const target =
      event.target instanceof HTMLElement
        ? event.target.closest("[data-log-kind],[data-log-older],[data-log-latest]")
        : null;
    if (!(target instanceof HTMLElement)) {
      return;
    }
    if (target.hasAttribute("data-log-kind")) {
      const picked = target.getAttribute("data-log-kind");
      logKind = picked === null || picked === "" ? null : picked;
      logHistory = false;
      pollNow(getToken());
      return;
    }
    if (target.hasAttribute("data-log-latest")) {
      logJournal = [];
      logJournalStart = 0;
      logJournalTotal = 0;
      logHistory = false;
      logFp = null;
      pollNow(getToken());
      return;
    }
    if (target.hasAttribute("data-log-older")) {
      pollOlder(getToken());
    }
  });
}

async function pollOlder(token) {
  const route = projectFromHash();
  if (route.name !== "log" || route.project === null) {
    return;
  }
  let reply = null;
  try {
    reply = await loadJournalOlder(route.project, logKind, logJournalStart, token);
  } catch {
    reply = null;
  }
  if (!(reply && reply.status === 200 && reply.data && Array.isArray(reply.data.entries))) {
    failures += 1;
    renderConnection({ live: false, seenMs });
    return;
  }
  const entries = reply.data.entries;
  if (entries.length === 0) {
    return;
  }
  const start = olderStart(logJournalStart);
  const overlap = Math.max(0, start + entries.length - logJournalStart);
  const capped = capJournal(entries.concat(logJournal.slice(overlap)), start);
  logJournal = capped.entries;
  logJournalStart = capped.start;
  if (typeof reply.data.total === "number") {
    logJournalTotal = reply.data.total;
  }
  logHistory = true;
  logFp = null;
  const view = lastLogView ? lastLogView.view : null;
  if (view !== null && !view.unknown && view.project !== null) {
    const next = { ...view, journal: logJournal, journalStart: logJournalStart, journalTotal: logJournalTotal, history: true };
    lastLogView = { key: lastLogView.key, view: next };
    try {
      renderLog(next, { nowMs: Date.now(), notice: lastNotice });
    } catch {
      failures += 1;
      renderConnection({ live: false, seenMs });
    }
  }
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
  bindLogView(
    () => token,
    (current) => {
      void tick(config, current);
    },
  );
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
