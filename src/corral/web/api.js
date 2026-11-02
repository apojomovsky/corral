// Data access for the shell: fetch, auth, backend identity. No DOM here.

export const TOKEN_KEY = "corral.token";

export function loadToken() {
  try {
    return window.localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function saveToken(value) {
  window.localStorage.setItem(TOKEN_KEY, value);
}

export function forgetToken() {
  window.localStorage.removeItem(TOKEN_KEY);
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
