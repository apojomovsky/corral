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

// Canonical JSON: the same object graph always stringifies to the
// same text, so idle polls compare stable state instead of key
// order. Used with stripVolatile above for the compare-and-skip
// path; undefined reads as null the way JSON.stringify renders it
// inside arrays, and stays absent inside objects the same way.
export function canonicalJson(value) {
  if (Array.isArray(value)) {
    return "[" + value.map(canonicalJson).join(",") + "]";
  }
  if (value !== null && typeof value === "object") {
    const parts = [];
    for (const key of Object.keys(value).sort()) {
      if (value[key] !== undefined) {
        parts.push(JSON.stringify(key) + ":" + canonicalJson(value[key]));
      }
    }
    return "{" + parts.join(",") + "}";
  }
  const text = JSON.stringify(value);
  return text === undefined ? "null" : text;
}

// A reset epoch as wall-clock hours and minutes for the quota bars.
// Null reads as not reported; anything unparseable is shown verbatim,
// the same degradation rule as the timestamp parsers above.
export function formatClock(value) {
  const ms = parseEpoch(value);
  if (ms === null || ms === undefined) {
    return "not reported";
  }
  if (typeof ms !== "number") {
    return String(ms);
  }
  const at = new Date(ms);
  const pad = (n) => String(n).padStart(2, "0");
  return pad(at.getHours()) + ":" + pad(at.getMinutes());
}

// A provider reading rendered in plain words: its age, and whether
// the server counts it as stale. Anything missing reads as never
// recorded, never as a fresh zero.
export function formatQuotaRead(info, nowMs) {
  if (info === null || info === undefined || typeof info !== "object") {
    return "no reading yet";
  }
  const readAt = parseEpoch(info.read_at);
  if (readAt === null || readAt === undefined) {
    return "no reading yet";
  }
  if (typeof readAt !== "number") {
    return "read " + String(readAt);
  }
  const aged = formatAge(readAt, nowMs);
  return info.stale ? "read " + aged + ", stale" : "read " + aged;
}

// How long until a stamp, in plain words. Null reads as not reported;
// a stamp already past reads as due now, never as a negative wait.
export function formatCountdown(targetMs, nowMs) {
  if (targetMs === null || targetMs === undefined) {
    return "not reported";
  }
  if (typeof targetMs !== "number" || typeof nowMs !== "number") {
    return String(targetMs);
  }
  if (targetMs <= nowMs) {
    return "due now";
  }
  const secs = Math.round((targetMs - nowMs) / 1000);
  const days = Math.floor(secs / 86400);
  const hours = Math.floor((secs % 86400) / 3600);
  const mins = Math.floor((secs % 3600) / 60);
  // Non-breaking spaces keep a countdown on one line: "3 d 4 h"
  // never wraps mid-unit on a narrow screen.
  const gap = "\u00a0";
  if (days > 0) {
    return hours > 0
      ? "in" + gap + days + gap + "d" + gap + hours + gap + "h"
      : "in" + gap + days + gap + "d";
  }
  if (hours > 0) {
    return mins > 0
      ? "in" + gap + hours + gap + "h" + gap + mins + gap + "m"
      : "in" + gap + hours + gap + "h";
  }
  if (mins > 0) {
    return "in" + gap + mins + gap + "m";
  }
  return "in" + gap + secs + gap + "s";
}

// A quota window reset as local wall-clock time plus the wait in
// words. The clock is local time; the countdown says how far away.
export function formatResetLine(resetsAt, nowMs) {
  const target = parseEpoch(resetsAt);
  if (target === null || target === undefined) {
    return "reset time not reported";
  }
  if (typeof target !== "number") {
    return "resets " + String(target);
  }
  const wait = formatCountdown(target, nowMs);
  if (wait === "due now") {
    return "resets " + formatClock(resetsAt) + " local, due now";
  }
  return "resets " + formatClock(resetsAt) + " local, " + wait;
}

// Millicores spelled out as cores too, so a bare 2000 reads as 2
// cores. Null reads as not reported; anything unparseable is shown
// verbatim, the timestamp formatters' degradation rule.
export function formatCpu(cpuM) {
  if (cpuM === null || cpuM === undefined) {
    return "not reported";
  }
  if (typeof cpuM !== "number" || !Number.isFinite(cpuM)) {
    return String(cpuM);
  }
  const cores = cpuM / 1000;
  const shown = Number.isInteger(cores) ? String(cores) : cores.toFixed(1);
  return shown + (cores === 1 ? " core" : " cores") + " (" + String(cpuM) + " millicores)";
}

// Megabytes spelled out as gigabytes too. Same degradation rule as
// formatCpu above.
export function formatMem(memoryMb) {
  if (memoryMb === null || memoryMb === undefined) {
    return "not reported";
  }
  if (typeof memoryMb !== "number" || !Number.isFinite(memoryMb)) {
    return String(memoryMb);
  }
  return (memoryMb / 1024).toFixed(1) + " GB (" + String(memoryMb) + " MB)";
}

// A drive pass exit code in words. The loop's own convention: 0 means
// the pass finished, 1 that there was nothing to do, 2 that another
// pass held the lock, 3 that a service could not be reached, 4 a
// usage or settings error. Null means no pass end was recorded.
export function exitWord(code) {
  if (code === null || code === undefined) {
    return "not recorded yet";
  }
  if (code === 0) {
    return "finished cleanly";
  }
  if (code === 1) {
    return "nothing to do";
  }
  if (code === 2) {
    return "another pass was already running";
  }
  if (code === 3) {
    return "a service could not be reached";
  }
  if (code === 4) {
    return "bad configuration or usage";
  }
  return "exit " + String(code);
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
