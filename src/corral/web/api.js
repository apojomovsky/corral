// Data access for the shell: fetch, auth, backend identity. No DOM here.

export const TOKEN_KEY = "corral.token";

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
