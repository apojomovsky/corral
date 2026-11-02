// Pure formatters: ages, sizes, percents, text cleaning. No DOM here.

// The volatile stamps the server restamps on every build. Kept equal
// to the server set by the suite, so idle polls compare stable state.
export const VOLATILE_KEYS = new Set(["now", "read_at"]);

export function stripVolatile(obj) {
  if (Array.isArray(obj)) {
    return obj.map(stripVolatile);
  }
  if (obj !== null && typeof obj === "object") {
    const out = {};
    for (const [key, value] of Object.entries(obj)) {
      if (!VOLATILE_KEYS.has(key)) {
        out[key] = stripVolatile(value);
      }
    }
    return out;
  }
  return obj;
}

// One timestamp parser per format, chosen per field, never sniffed:
// epoch seconds as JSON numbers, ISO 8601 strings otherwise. Null
// reads as not reported; anything unparseable is returned as-is so a
// format surprise degrades to legible text instead of a blank.
export function parseEpoch(value) {
  if (value === null || value === undefined) {
    return null;
  }
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return value;
  }
  return value * 1000;
}

export function parseIso(value) {
  if (value === null || value === undefined) {
    return null;
  }
  if (typeof value !== "string") {
    return value;
  }
  const ms = Date.parse(value);
  return Number.isNaN(ms) ? value : ms;
}

export function formatAge(stampMs, nowMs) {
  if (stampMs === null || stampMs === undefined) {
    return "not reported";
  }
  if (typeof stampMs !== "number") {
    return String(stampMs);
  }
  const secs = Math.max(0, Math.round((nowMs - stampMs) / 1000));
  if (secs < 5) {
    return "just now";
  }
  if (secs < 60) {
    return secs + "s ago";
  }
  const mins = Math.floor(secs / 60);
  if (mins < 60) {
    return mins + "m ago";
  }
  const hours = Math.floor(mins / 60);
  if (hours < 48) {
    return hours + "h ago";
  }
  return Math.floor(hours / 24) + "d ago";
}

export function formatBytes(value) {
  if (value === null || value === undefined) {
    return "not reported";
  }
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return String(value);
  }
  const units = ["B", "KB", "MB", "GB", "TB"];
  let n = value;
  let unit = 0;
  while (n >= 1024 && unit < units.length - 1) {
    n /= 1024;
    unit += 1;
  }
  return (unit === 0 ? Math.round(n) : n.toFixed(1)) + " " + units[unit];
}

export function formatPct(value) {
  if (value === null || value === undefined) {
    return "no reading";
  }
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return String(value);
  }
  return Math.round(value) + "%";
}

// Presentation hygiene, not a sanitizer: the text-node rule is the
// actual boundary. Drops control characters other than newline and
// tab, plus the bidirectional override range, so stray terminal
// bytes cannot mislead the layout.
export function cleanText(value) {
  if (typeof value !== "string") {
    return "";
  }
  return value.replace(
    /[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F\u202A-\u202E\u2066-\u2069\u200E\u200F]/g,
    "",
  );
}
