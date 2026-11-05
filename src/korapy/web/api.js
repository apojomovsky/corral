// Data access for the shell: fetch, auth, backend identity. No DOM here.

export const TOKEN_KEY = "korapy.token";

// Poll cadence and failure backoff, in milliseconds. One pure table:
// the delay after N consecutive failures is BACKOFF_MS[N-1], capped
// at the last entry. Kept here (not in app.js) so the suite can pin
// the numbers without a JS runner.
export const POLL_MS = 5000;
export const BACKOFF_MS = [5000, 15000, 30000, 60000];

export function nextDelay(failures) {
  if (failures <= 0) {
    return POLL_MS;
  }
  return BACKOFF_MS[Math.min(failures - 1, BACKOFF_MS.length - 1)];
}

export function loadToken() {
  try {
    return window.localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

// False when this browser refuses the write (private mode): the
// caller keeps the token for the current page and says so on screen.
export function saveToken(value) {
  try {
    window.localStorage.setItem(TOKEN_KEY, value);
    return true;
  } catch {
    return false;
  }
}

export function forgetToken() {
  try {
    window.localStorage.removeItem(TOKEN_KEY);
    return true;
  } catch {
    return false;
  }
}

async function readJson(resp) {
  const text = await resp.text();
  if (!text) {
    return null;
  }
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

export async function fetchJson(path, token, { timeoutMs = 10000 } = {}) {
  const ctrl = new AbortController();
  const timer = window.setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const headers = {};
    if (token) {
      headers["Authorization"] = "Bearer " + token;
    }
    const resp = await window.fetch(path, {
      cache: "no-store",
      headers,
      signal: ctrl.signal,
    });
    return { status: resp.status, data: await readJson(resp) };
  } catch {
    // Unreachable server, refused connection, timeout: a status the
    // HTTP layer can never send, so every caller takes the failure
    // branch (backoff, stale banner, reschedule) without its own throw.
    return { status: 0, data: null };
  } finally {
    window.clearTimeout(timer);
  }
}

// The unauthed backend identity: mode and api major. Needs no token,
// so the shell and the token screen know which backend they face
// before any credential exists.
export function loadConfig() {
  return fetchJson("/ui/config.json", null);
}

// Proof a token works. Host mode checks the version route; a wrong
// token answers 401 there without leaking anything.
export function loadVersion(token) {
  return fetchJson("/v1/version", token);
}

// The whole fleet document for the overview poll.
export function loadSnapshot(token) {
  return fetchJson("/v1/snapshot", token);
}

// Board list cadence and page: the list view polls every 15s, 50 rows
// per request, every request slimmed with body=0 and bounded by a
// 300s freshness cap the view chose. Named here (not in the view) so
// the suite can pin the numbers without a JS runner.
export const BOARD_POLL_MS = 15000;
export const BOARD_PAGE_SIZE = 50;
export const BOARD_MAX_AGE_S = 300;

// One page of board rows for the list view: slimmed, freshness-capped
// and paged. Filters map to the route's own query parameters.
export function loadBoard(
  token,
  { repo = null, state = null, label = null, limit = BOARD_PAGE_SIZE, offset = 0 } = {},
) {
  const params = new URLSearchParams();
  params.set("body", "0");
  params.set("max_age", String(BOARD_MAX_AGE_S));
  params.set("limit", String(limit));
  params.set("offset", String(offset));
  if (repo) {
    params.set("repo", repo);
  }
  if (state) {
    params.set("state", state);
  }
  if (label) {
    params.set("label", label);
  }
  return fetchJson("/v1/board?" + params.toString(), token);
}

// The board poll delay: the list cadence on success, the shared
// backoff once polls fail, capped at the last entry.
export function nextBoardDelay(failures) {
  if (failures <= 0) {
    return BOARD_POLL_MS;
  }
  return BACKOFF_MS[Math.min(failures - 1, BACKOFF_MS.length - 1)];
}

// The agent-detail tail cadence, in milliseconds. Failures back off
// on the shared table; successes stay on this cadence.
export const TAIL_POLL_MS = 3000;

async function readText(resp) {
  try {
    return await resp.text();
  } catch {
    return null;
  }
}

export async function fetchText(path, token, { timeoutMs = 10000 } = {}) {
  const ctrl = new AbortController();
  const timer = window.setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const headers = {};
    if (token) {
      headers["Authorization"] = "Bearer " + token;
    }
    const resp = await window.fetch(path, {
      cache: "no-store",
      headers,
      signal: ctrl.signal,
    });
    return { status: resp.status, text: await readText(resp) };
  } catch {
    // Same contract as fetchJson: a status the HTTP layer can never
    // send, so the caller takes the failure branch without its own
    // throw and the poll loop survives.
    return { status: 0, text: null };
  } finally {
    window.clearTimeout(timer);
  }
}

// One ticket's activity lines for the detail tail pane.
export function loadTail(token, repo, number) {
  return fetchText(
    "/v1/tail?repo=" + encodeURIComponent(repo) + "&number=" + encodeURIComponent(String(number)),
    token,
  );
}

// Whether a row names a ticket. Ticket-less rows (null repo or
// number, which the server allows) get a fields-only detail.
export function hasTicket(row) {
  return (
    !!row &&
    row.repo !== null &&
    row.repo !== undefined &&
    row.number !== null &&
    row.number !== undefined
  );
}

// The detail key for a row: its id when it has one, else its ticket
// plus role, so rows sharing one ticket still get their own detail.
export function agentKey(row) {
  if (!row || typeof row !== "object") {
    return null;
  }
  if (row.id !== null && row.id !== undefined && row.id !== "") {
    return { kind: "id", id: String(row.id) };
  }
  if (hasTicket(row)) {
    return {
      kind: "ticket",
      repo: String(row.repo),
      number: String(row.number),
      role: row.role === null || row.role === undefined ? null : String(row.role),
    };
  }
  return null;
}

export function detailHash(key) {
  if (key.kind === "id") {
    return "#/agent?id=" + encodeURIComponent(key.id);
  }
  let hash =
    "#/agent?repo=" + encodeURIComponent(key.repo) + "&number=" + encodeURIComponent(key.number);
  if (key.role !== null) {
    hash += "&role=" + encodeURIComponent(key.role);
  }
  return hash;
}

// The row a detail key names in a fresh document, or null when
// nothing matches. Never a guessed neighbour.
export function resolveAgent(doc, key) {
  const rows = doc && Array.isArray(doc.agents) ? doc.agents : [];
  if (!key || typeof key !== "object") {
    return null;
  }
  for (const row of rows) {
    if (!row || typeof row !== "object") {
      continue;
    }
    if (key.kind === "id") {
      if (row.id !== null && row.id !== undefined && String(row.id) === key.id) {
        return row;
      }
    } else if (key.kind === "ticket") {
      if (!hasTicket(row)) {
        continue;
      }
      if (String(row.repo) !== key.repo || String(row.number) !== key.number) {
        continue;
      }
      if (
        key.role !== null &&
        (row.role === null || row.role === undefined || String(row.role) !== key.role)
      ) {
        continue;
      }
      return row;
    }
  }
  return null;
}

// Milliseconds between version rechecks while the page stays open.
// The API major is compared at each unlock, then on this cadence
// and whenever the page becomes visible again.
export const VERSION_CHECK_MS = 60000;

// Whether a snapshot section is the server's bare failure body,
// mirrored from the server's own rule so degraded state renders as
// an error card instead of a blank.
export function isErrorSection(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }
  const keys = Object.keys(value);
  return keys.length === 1 && keys[0] === "error";
}

// The project chips: the union of agent-row projects and counted
// capacity projects, the same derivation in both modes. Null and
// empty names never become chips; those agents show under All only.
export function snapshotProjects(doc) {
  const names = new Set();
  const agents = doc && Array.isArray(doc.agents) ? doc.agents : [];
  for (const row of agents) {
    if (row && typeof row.project === "string" && row.project !== "") {
      names.add(row.project);
    }
  }
  const capacity = doc && doc.capacity && !isErrorSection(doc.capacity) ? doc.capacity : null;
  const counted = capacity && capacity.by_project ? Object.keys(capacity.by_project) : [];
  for (const name of counted) {
    if (name !== "") {
      names.add(name);
    }
  }
  return [...names].sort();
}

// The document gate: a newer schema proceeds (additive), an older
// or missing one renders as unsupported, never as a guess.
export function isSupportedDocument(doc, expectedSchema) {
  return (
    !!doc &&
    typeof doc.schema_version === "number" &&
    doc.schema_version >= expectedSchema
  );
}

// The API gate: a different major stops rendering data until the
// operator upgrades and reloads.
export function isSupportedApi(version, expectedApi) {
  return !!version && version.api === expectedApi;
}

// Log view (host mode) data access. The log routes take exactly one
// project; "All" never merges logs, it shows the project picker.

// The log poll cadence and the render bounds of section 2: the board
// and the log poll every 15s; the events stack renders at most 100
// rows with the true unresolved count stated when capped; journal
// pages are 100 rows and at most 300 nodes stay rendered.
export const LOG_POLL_MS = 15000;
export const LOG_JOURNAL_PAGE = 100;
export const LOG_JOURNAL_CAP = 300;
export const LOG_EVENTS_CAP = 100;

// The kind filter chips: exactly the tuple the journal route
// validates against, so a chip can never answer 400. Pinned equal
// by the suite, since the script cannot import the server tuple.
export const LOG_KINDS = ["decision", "action", "incident", "followup", "done", "handoff", "note"];

// The log backoff: the 15s cadence is the floor, the shared table
// the ceiling, so a failing log view backs off 15, 15, 30, 60s.
export function nextLogDelay(failures) {
  if (failures <= 0) {
    return LOG_POLL_MS;
  }
  return Math.max(LOG_POLL_MS, nextDelay(failures));
}

// The unresolved stack for one project, oldest first, unpaged: the
// view caps what it renders and states the true count when capped.
export function loadEvents(project, token) {
  return fetchJson("/v1/events?project=" + encodeURIComponent(project), token);
}

function journalPath(project, kind, limit, cursor) {
  let path = "/v1/journal?project=" + encodeURIComponent(project);
  if (kind) {
    path += "&kind=" + encodeURIComponent(kind);
  }
  if (limit !== null && limit !== undefined) {
    path += "&limit=" + encodeURIComponent(String(limit));
  }
  if (cursor !== null && cursor !== undefined) {
    path += "&cursor=" + encodeURIComponent(String(cursor));
  }
  return path;
}

export function loadJournal(project, kind, limit, cursor, token) {
  return fetchJson(journalPath(project, kind, limit, cursor), token);
}

// The newest journal window. The journal pages oldest first, so the
// view learns `total` from a one-row reply and then requests the
// trailing window by index. A future cursor reminting breaks the
// suite pin first, not a browser.
export async function loadJournalLast(project, kind, token) {
  let path = "/v1/journal?project=" + encodeURIComponent(project);
  if (kind) {
    path += "&kind=" + encodeURIComponent(kind);
  }
  const first = await fetchJson(path + "&limit=1", token);
  if (first.status !== 200 || !first.data || typeof first.data.total !== "number") {
    return first;
  }
  const total = first.data.total;
  if (total <= 1) {
    return first;
  }
  const start = Math.max(0, total - LOG_JOURNAL_PAGE);
  const windowReply = await fetchJson(
    path + "&limit=" + String(LOG_JOURNAL_PAGE) + "&cursor=" + String(start),
    token,
  );
  if (
    windowReply.status !== 200 ||
    !windowReply.data ||
    typeof windowReply.data.total !== "number"
  ) {
    return windowReply;
  }
  // Entries arriving between the probe and the window shift the
  // index; recompute start from the window reply's own total and
  // refetch once when it moved.
  const latest = windowReply.data.total;
  const corrected = Math.max(0, latest - LOG_JOURNAL_PAGE);
  if (corrected === start) {
    return windowReply;
  }
  return fetchJson(
    path + "&limit=" + String(LOG_JOURNAL_PAGE) + "&cursor=" + String(corrected),
    token,
  );
}

// The previous journal window above `start`: the "load older" page.
// `start` is the index of the oldest rendered entry.
export function loadJournalOlder(project, kind, start, token) {
  return loadJournal(project, kind, LOG_JOURNAL_PAGE, olderStart(start), token);
}

// The index arithmetic the log view needs without naming the page
// size outside this module.
export function olderStart(start) {
  return Math.max(0, start - LOG_JOURNAL_PAGE);
}

// The registered names, names only, behind the same auth as data.
export function loadProjects(token) {
  return fetchJson("/v1/projects", token);
}

// The offered project list: the route answer unioned with the set
// derived from activity, never intersected, so a project missing
// from either source is still offered.
export function projectNames(registered, derived) {
  const names = new Set();
  for (const list of [registered, derived]) {
    if (Array.isArray(list)) {
      for (const name of list) {
        if (typeof name === "string" && name !== "") {
          names.add(name);
        }
      }
    }
  }
  return [...names].sort();
}

// The rendered events, capped: the true count travels beside the
// capped rows so the view can state it when capped.
export function capEvents(rows) {
  return rows.slice(0, LOG_EVENTS_CAP);
}

// Drop the oldest rendered entries past the cap, keeping the newest
// window the operator was reading, with the start index adjusted.
export function capJournal(entries, start) {
  if (entries.length > LOG_JOURNAL_CAP) {
    const dropped = entries.length - LOG_JOURNAL_CAP;
    return { entries: entries.slice(dropped), start: start + dropped };
  }
  return { entries, start };
}
