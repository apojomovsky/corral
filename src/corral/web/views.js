// View renderers. Untrusted strings enter the DOM as text only,
// never as markup; this module sets textContent and nothing else.
// Bar geometry rides on SVG attributes, never CSS: the policy
// allows no inline style, so a width can only be an attribute.

import {
  LOG_EVENTS_CAP,
  LOG_JOURNAL_CAP,
  LOG_KINDS,
  isErrorSection,
  snapshotProjects,
} from "./api.js";
import {
  cleanText,
  formatAge,
  formatBytes,
  formatClock,
  formatPct,
  parseEpoch,
  parseIso,
} from "./fmt.js";

// The element namespace for inline charts. A namespace identifier,
// not a fetch: no request ever goes to it.
const SVG_NS = "http://www.w3.org/2000/svg";

const BAR_WIDTH = 120;
const BAR_HEIGHT = 10;

// el() sets attributes unchecked: never pass a tracker href through it
// without a scheme check (W6 must allow http/https only).
export function el(tag, text, attrs) {
  const node = document.createElement(tag);
  if (text !== null && text !== undefined) {
    node.textContent = cleanText(String(text));
  }
  for (const [name, value] of Object.entries(attrs || {})) {
    node.setAttribute(name, value);
  }
  return node;
}

export function renderShell({ mode, api, version, seenMs, serverNowMs }) {
  const modeNode = document.getElementById("mode");
  modeNode.textContent = mode + " mode";
  const apiNode = document.getElementById("api");
  apiNode.textContent = "api " + String(api);
  const line = document.getElementById("status-line");
  const parts = ["serving api " + String(api)];
  if (version && typeof version.schema_version === "number") {
    parts.push("document schema " + String(version.schema_version));
  }
  if (typeof serverNowMs === "number") {
    parts.push("server time " + formatAge(serverNowMs, seenMs));
  }
  line.textContent = "";
  line.appendChild(document.createTextNode(parts.join(", ")));
}

export function renderConnection({ live, seenMs }) {
  const conn = document.getElementById("conn");
  const updated = document.getElementById("updated");
  const view = document.getElementById("view");
  const banner = document.getElementById("banner");
  if (live) {
    conn.textContent = "live " + formatAge(seenMs, Date.now());
    updated.textContent = "";
    banner.hidden = true;
    banner.textContent = "";
    view.classList.remove("stale");
    return;
  }
  if (seenMs === null || seenMs === undefined) {
    // No good poll yet (the server was down at first load): there is
    // no stale data to age, only the retry the poll loop scheduled.
    conn.textContent = "still connecting";
    updated.textContent = "";
    banner.hidden = false;
    banner.textContent = "";
    banner.appendChild(document.createTextNode("Cannot reach the API yet. Retrying."));
    return;
  }
  conn.textContent = "cannot reach the api";
  updated.textContent = "showing data from " + formatAge(seenMs, Date.now());
  banner.hidden = false;
  banner.textContent = "";
  banner.appendChild(
    document.createTextNode(
      "Cannot reach the API. Showing data from " +
        formatAge(seenMs, Date.now()) +
        ".",
    ),
  );
  view.classList.add("stale");
}

export function showToken({ message, help }) {
  document.getElementById("token").hidden = false;
  const msg = document.getElementById("token-msg");
  msg.textContent = "";
  if (message) {
    msg.appendChild(document.createTextNode(cleanText(message)));
  }
  if (help) {
    const helpNode = document.getElementById("token-help");
    helpNode.textContent = "";
    helpNode.appendChild(document.createTextNode(cleanText(help)));
  }
}

export function hideToken() {
  document.getElementById("token").hidden = true;
  document.getElementById("token-msg").textContent = "";
}

function card(title, wrong) {
  const section = document.createElement("section");
  section.setAttribute("class", wrong ? "card error" : "card");
  section.appendChild(el("h2", title));
  return section;
}

function errorCard(title, body) {
  const text = body && typeof body.error === "string" ? body.error : "unreadable";
  const section = card(title, true);
  section.appendChild(el("p", text));
  return section;
}

function age(value, nowMs) {
  return formatAge(parseEpoch(value), nowMs);
}

function quotaBar(window) {
  const used = window && typeof window.used_pct === "number" ? window.used_pct : null;
  const line = el("span", formatPct(used) + " resets " + formatClock(window ? window.resets_at : null));
  if (used === null || !Number.isFinite(used)) {
    return line;
  }
  const pct = Math.min(100, Math.max(0, used));
  const wrap = document.createElement("span");
  wrap.setAttribute("class", "bar");
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("width", String(BAR_WIDTH));
  svg.setAttribute("height", String(BAR_HEIGHT));
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", formatPct(used) + " used");
  const back = document.createElementNS(SVG_NS, "rect");
  back.setAttribute("x", "0");
  back.setAttribute("y", "0");
  back.setAttribute("width", String(BAR_WIDTH));
  back.setAttribute("height", String(BAR_HEIGHT));
  back.setAttribute("fill", "none");
  back.setAttribute("stroke", "currentColor");
  const fill = document.createElementNS(SVG_NS, "rect");
  fill.setAttribute("x", "0");
  fill.setAttribute("y", "0");
  fill.setAttribute("width", String(Math.round((pct / 100) * BAR_WIDTH)));
  fill.setAttribute("height", String(BAR_HEIGHT));
  fill.setAttribute("fill", "currentColor");
  svg.appendChild(back);
  svg.appendChild(fill);
  wrap.appendChild(svg);
  wrap.appendChild(line);
  return wrap;
}

function quotaCard(quota, nowMs) {
  if (isErrorSection(quota)) {
    return errorCard("quota", quota);
  }
  const section = card("quota");
  const providers = quota && quota.providers ? quota.providers : {};
  const names = Object.keys(providers).sort();
  if (names.length === 0) {
    section.appendChild(el("p", "no providers"));
    return section;
  }
  for (const name of names) {
    const info = providers[name] || {};
    const bits = [String(info.state)];
    bits.push("read " + age(info.read_at, nowMs));
    if (info.stale) {
      bits.push("stale");
    }
    if (typeof info.error === "string" && info.error !== "") {
      bits.push(info.error);
    }
    section.appendChild(el("h3", name));
    section.appendChild(el("p", bits.join(", ")));
    const windows = info.windows && typeof info.windows === "object" ? info.windows : null;
    if (windows === null) {
      section.appendChild(el("p", "no reading"));
      continue;
    }
    const ids = Object.keys(windows).sort();
    if (ids.length === 0) {
      section.appendChild(el("p", "no reading"));
      continue;
    }
    const list = document.createElement("ul");
    for (const id of ids) {
      const item = document.createElement("li");
      item.appendChild(el("span", id + " "));
      const window = windows[id];
      item.appendChild(
        window && typeof window === "object" ? quotaBar(window) : el("span", "no reading"),
      );
      list.appendChild(item);
    }
    section.appendChild(list);
  }
  return section;
}

function usageTriple(triple) {
  // A new server shape can send a null share: read it as unknown
  // instead of throwing inside the poll loop.
  const info = triple !== null && typeof triple === "object" ? triple : {};
  const cpu = info.cpu_m === null || info.cpu_m === undefined ? "?" : String(info.cpu_m);
  const mem = info.memory_mb === null || info.memory_mb === undefined ? "?" : String(info.memory_mb);
  const pids = info.pids === null || info.pids === undefined ? "?" : String(info.pids);
  return "cpu " + cpu + "m, mem " + mem + " MB, pids " + pids;
}

function measuredText(measured) {
  const bits = [];
  if (measured.cpu_usage_usec !== null && measured.cpu_usage_usec !== undefined) {
    bits.push("cpu " + (measured.cpu_usage_usec / 1000000).toFixed(1) + "s");
  }
  if (measured.memory_mb !== null && measured.memory_mb !== undefined) {
    bits.push("mem " + String(measured.memory_mb) + " MB");
  }
  if (measured.pids !== null && measured.pids !== undefined) {
    bits.push("pids " + String(measured.pids));
  }
  return bits.join(", ");
}

// A pressure reading outside the contract degrades to its raw value
// as text, the timestamp formatters' rule, instead of NaN.
function pressureValue(value, digits) {
  const num = Number(value);
  return Number.isFinite(num) ? num.toFixed(digits) : String(value);
}

function pressureText(pressure) {
  const info = pressure && typeof pressure === "object" ? pressure : {};
  const bits = [];
  if (info.load_per_cpu !== null && info.load_per_cpu !== undefined) {
    bits.push("load " + pressureValue(info.load_per_cpu, 2) + "/cpu");
  }
  if (info.mem_available_pct !== null && info.mem_available_pct !== undefined) {
    bits.push("mem " + pressureValue(info.mem_available_pct, 1) + "% avail");
  }
  if (bits.length === 0) {
    return "pressure unknown";
  }
  bits.push(info.saturated ? "saturated" : "ok");
  return bits.join(", ");
}

function resourcesCard(resources) {
  if (isErrorSection(resources)) {
    return errorCard("resources", resources);
  }
  const section = card("resources");
  const info = resources && typeof resources === "object" ? resources : {};
  const charged = info.charged && typeof info.charged === "object" ? info.charged : null;
  const budget = info.host_budget && typeof info.host_budget === "object" ? info.host_budget : null;
  if (charged === null) {
    section.appendChild(el("p", "no reading"));
    return section;
  }
  if (budget === null) {
    section.appendChild(el("p", "charged " + usageTriple(charged) + " (no host budget)"));
  } else {
    const parts = [];
    const dims = [
      ["cpu_m", "cpu", "m"],
      ["memory_mb", "mem", " MB"],
      ["pids", "pids", ""],
    ];
    for (const [dim, short, unit] of dims) {
      const used = charged[dim];
      const whole = budget[dim];
      parts.push(
        whole === null || whole === undefined
          ? short + " " + String(used) + unit
          : short + " " + String(used) + "/" + String(whole) + unit,
      );
    }
    section.appendChild(el("p", "charged " + parts.join(", ")));
  }
  const shares = info.by_project && typeof info.by_project === "object" ? info.by_project : {};
  const measured =
    info.measured_by_project && typeof info.measured_by_project === "object"
      ? info.measured_by_project
      : {};
  const lines = document.createElement("ul");
  for (const name of Object.keys(shares).sort()) {
    let text = (name === "" ? "-" : name) + ": charged " + usageTriple(shares[name]);
    const got = measured[name];
    if (got !== null && got !== undefined && typeof got === "object") {
      const shown = measuredText(got);
      if (shown !== "") {
        text += "; measured " + shown;
      }
    }
    const item = document.createElement("li");
    item.appendChild(el("span", text));
    lines.appendChild(item);
  }
  if (lines.childNodes.length > 0) {
    section.appendChild(lines);
  }
  section.appendChild(el("p", pressureText(info.pressure)));
  const notes = Array.isArray(info.notes) ? info.notes : [];
  for (const note of notes) {
    section.appendChild(el("p", "note: " + String(note)));
  }
  return section;
}

function capacityCard(capacity) {
  if (isErrorSection(capacity)) {
    return errorCard("capacity", capacity);
  }
  const section = card("capacity");
  const info = capacity && typeof capacity === "object" ? capacity : {};
  const shares = info.by_project && typeof info.by_project === "object" ? info.by_project : {};
  const bits = Object.keys(shares)
    .sort()
    .map((name) => (name === "" ? "-" : name) + ":" + String(shares[name]));
  section.appendChild(
    el(
      "p",
      String(info.live) +
        "/" +
        String(info.total_cap) +
        " (per repo " +
        String(info.repo_cap) +
        ")" +
        (bits.length > 0 ? " " + bits.join(" ") : ""),
    ),
  );
  return section;
}

function diskCard(disk) {
  if (isErrorSection(disk)) {
    return errorCard("disk", disk);
  }
  const section = card("disk");
  const info = disk && typeof disk === "object" ? disk : {};
  const paths = Array.isArray(info.paths) ? info.paths : [];
  if (paths.length === 0) {
    section.appendChild(el("p", "no paths"));
    return section;
  }
  const list = document.createElement("ul");
  for (const item of paths) {
    const row = document.createElement("li");
    if (item && typeof item.error === "string") {
      row.appendChild(el("span", String(item.path) + ": " + item.error));
    } else if (item) {
      row.appendChild(
        el("span", formatBytes(item.free) + " free of " + formatBytes(item.total) + " (" + String(item.path) + ")"),
      );
    }
    list.appendChild(row);
  }
  section.appendChild(list);
  return section;
}

function driveCard(drive, nowMs) {
  if (isErrorSection(drive)) {
    return errorCard("drive", drive);
  }
  const section = card("drive");
  const info = drive && typeof drive === "object" ? drive : {};
  const state = info.paused
    ? "paused" + (info.pause_reason ? " (" + String(info.pause_reason) + ")" : "")
    : "running";
  section.appendChild(
    el(
      "p",
      state +
        ", last pass " +
        age(info.last_pass_at, nowMs) +
        ", exit " +
        (info.last_exit === null || info.last_exit === undefined ? "not reported" : String(info.last_exit)) +
        ", starts " +
        String(info.starts_today) +
        "/" +
        String(info.max_starts_per_day),
    ),
  );
  return section;
}

function agentText(row, nowMs) {
  const ticket =
    row.repo === null || row.repo === undefined || row.number === null || row.number === undefined
      ? "no ticket"
      : String(row.repo) + "#" + String(row.number);
  const bits = [ticket, String(row.state)];
  if (row.runtime !== null && row.runtime !== undefined) {
    bits.push(String(row.runtime));
  }
  if (row.model !== null && row.model !== undefined) {
    bits.push(String(row.model));
  }
  if (row.role !== null && row.role !== undefined) {
    bits.push(String(row.role));
  }
  if (row.started_at !== null && row.started_at !== undefined) {
    bits.push("started " + age(row.started_at, nowMs));
  }
  bits.push("seen " + age(row.last_seen, nowMs));
  return { line: bits.join("  "), note: row.note };
}

function agentsSection(agents, project, nowMs) {
  if (isErrorSection(agents)) {
    return errorCard("agents", agents);
  }
  const section = card("agents");
  const rows = Array.isArray(agents) ? agents : [];
  const kept = rows.filter(
    (row) => row !== null && typeof row === "object" && (project === null || row.project === project),
  );
  if (kept.length === 0) {
    section.appendChild(el("p", project === null ? "no agents" : "no agents in this project"));
    return section;
  }
  // Server order is already (project, repo, number, id); regroup
  // without re-sorting so rows never jump between polls.
  const groups = new Map();
  for (const row of kept) {
    const key = project === null ? String(row.project || "") : project;
    if (!groups.has(key)) {
      groups.set(key, []);
    }
    groups.get(key).push(row);
  }
  for (const [name, group] of [...groups.entries()].sort((a, b) => (a[0] < b[0] ? -1 : 1))) {
    if (project !== null) {
      section.appendChild(el("h3", project + " (" + String(group.length) + ")"));
    } else if (name !== "") {
      section.appendChild(el("h3", name + " (" + String(group.length) + ")"));
    } else {
      section.appendChild(el("h3", "no project (" + String(group.length) + ")"));
    }
    const list = document.createElement("ul");
    for (const row of group) {
      const item = document.createElement("li");
      const shown = agentText(row, nowMs);
      item.appendChild(el("span", shown.line));
      if (shown.note !== null && shown.note !== undefined && String(shown.note) !== "") {
        item.appendChild(el("span", "  note: " + String(shown.note)));
      }
      list.appendChild(item);
    }
    section.appendChild(list);
  }
  return section;
}

export function renderChips(projects, selected, base) {
  const nav = document.getElementById("chips");
  nav.textContent = "";
  const all = document.createElement("a");
  if (base === "#/log") {
    all.setAttribute("href", "#/log");
  } else {
    all.setAttribute("href", "#/");
  }
  all.textContent = "All";
  if (selected === null) {
    all.setAttribute("aria-current", "true");
  }
  nav.appendChild(all);
  for (const name of projects) {
    const chip = document.createElement("a");
    if (base === "#/log") {
      chip.setAttribute("href", "#/log?project=" + encodeURIComponent(name));
    } else {
      chip.setAttribute("href", "#/?project=" + encodeURIComponent(name));
    }
    chip.textContent = cleanText(name);
    if (name === selected) {
      chip.setAttribute("aria-current", "true");
    }
    nav.appendChild(chip);
  }
}

// The view tabs. The board arrives with its own view and hub mode
// adds hosts; each view renders its own tab as current.
export function renderTabs(current) {
  const nav = document.getElementById("tabs");
  nav.textContent = "";
  const tabs = [
    ["#/", "Overview", "overview"],
    ["#/log", "Log", "log"],
  ];
  for (const [href, label, name] of tabs) {
    const link = document.createElement("a");
    link.setAttribute("href", href);
    link.textContent = label;
    if (name === current) {
      link.setAttribute("aria-current", "true");
    }
    nav.appendChild(link);
  }
}

function headerLine(header) {
  if (isErrorSection(header)) {
    return errorCard("fleet", header);
  }
  const info = header && typeof header === "object" ? header : {};
  const section = card("fleet");
  section.appendChild(
    el(
      "p",
      "Agents: " +
        String(info.agents_live) +
        " live, " +
        String(info.agents_stale) +
        " stale, " +
        String(info.agents_finished) +
        " finished",
    ),
  );
  return section;
}

// The overview: header counts, one card per snapshot section
// (failed sections as error cards, never hidden), then agents
// grouped by project under the selected filter.
export function renderOverview(doc, { project, nowMs, notice }) {
  renderTabs("overview");
  renderChips(snapshotProjects(doc), project);
  const note = document.getElementById("notice");
  note.textContent = "";
  if (notice) {
    note.hidden = false;
    note.appendChild(document.createTextNode(cleanText(notice)));
  } else {
    note.hidden = true;
  }
  const cards = document.getElementById("cards");
  cards.textContent = "";
  cards.appendChild(headerLine(doc.fleet_header));
  cards.appendChild(quotaCard(doc.quota, nowMs));
  cards.appendChild(capacityCard(doc.capacity));
  cards.appendChild(resourcesCard(doc.resources));
  cards.appendChild(diskCard(doc.disk));
  cards.appendChild(driveCard(doc.drive, nowMs));
  const agents = document.getElementById("agents");
  agents.textContent = "";
  agents.appendChild(agentsSection(doc.agents, project, nowMs));
}

function eventLine(row, nowMs) {
  const kind = row.kind === null || row.kind === undefined ? "unknown" : String(row.kind);
  const ref = row.ref === null || row.ref === undefined ? "no ref" : String(row.ref);
  const raw = row.evidence === null || row.evidence === undefined ? "" : String(row.evidence);
  const evidence = raw === "" ? "no evidence" : raw;
  return kind + " " + ref + ": " + evidence + " (" + formatAge(parseIso(row.first_seen), nowMs) + ")";
}

function eventsCard(view, nowMs) {
  const total = typeof view.eventsTotal === "number" ? view.eventsTotal : view.events.length;
  const section = card("needs attention (" + String(total) + ")");
  if (view.eventsState === "missing") {
    section.appendChild(el("p", "Choose a project to see its log."));
    return section;
  }
  if (view.eventsState === "unknown") {
    section.appendChild(el("p", "Unknown project " + String(view.project) + "."));
    return section;
  }
  if (view.eventsState === "failed") {
    section.appendChild(el("p", "Cannot load events right now; the next poll retries."));
    return section;
  }
  if (view.events.length === 0) {
    section.appendChild(el("p", "No unresolved events."));
    return section;
  }
  const list = document.createElement("ul");
  for (const row of view.events) {
    const item = document.createElement("li");
    item.appendChild(el("span", eventLine(row, nowMs)));
    list.appendChild(item);
  }
  section.appendChild(list);
  if (total > view.events.length) {
    section.appendChild(
      el("p", "Showing " + String(view.events.length) + " of " + String(total) + "."),
    );
  }
  return section;
}

function kindChips(selected) {
  const wrap = document.createElement("div");
  wrap.setAttribute("class", "kinds");
  const all = document.createElement("button");
  all.setAttribute("type", "button");
  all.setAttribute("data-log-kind", "");
  all.textContent = "all";
  if (selected === null) {
    all.setAttribute("aria-current", "true");
  }
  wrap.appendChild(all);
  for (const kind of LOG_KINDS) {
    const chip = document.createElement("button");
    chip.setAttribute("type", "button");
    chip.setAttribute("data-log-kind", kind);
    chip.textContent = cleanText(kind);
    if (kind === selected) {
      chip.setAttribute("aria-current", "true");
    }
    wrap.appendChild(chip);
  }
  return wrap;
}

function journalEntryNode(row, nowMs) {
  const kind = row.kind === null || row.kind === undefined ? "unknown" : String(row.kind);
  const holder = row.holder === null || row.holder === undefined ? "unknown" : String(row.holder);
  const item = document.createElement("li");
  item.appendChild(el("span", formatAge(parseIso(row.at), nowMs) + " " + kind + " " + holder));
  const refs = Array.isArray(row.refs) ? row.refs : [];
  if (refs.length > 0) {
    item.appendChild(el("span", " refs: " + refs.map((ref) => String(ref)).join(", ")));
  }
  const text = row.text === null || row.text === undefined ? "" : String(row.text);
  item.appendChild(el("p", text === "" ? "(empty entry)" : text));
  return item;
}

function journalCard(view, nowMs) {
  const total = typeof view.journalTotal === "number" ? view.journalTotal : 0;
  const section = card("journal (" + String(total) + ")");
  if (view.journalState === "missing") {
    section.appendChild(el("p", "Choose a project above to read its logbook."));
    return section;
  }
  if (view.journalState === "unknown") {
    section.appendChild(el("p", "Unknown project " + String(view.project) + "."));
    return section;
  }
  section.appendChild(kindChips(view.kind));
  if (view.journalState === "failed") {
    section.appendChild(el("p", "Cannot load the journal right now; the next poll retries."));
    return section;
  }
  if (view.journal.length === 0) {
    section.appendChild(el("p", "No entries yet."));
    return section;
  }
  const list = document.createElement("ul");
  for (const row of view.journal) {
    list.appendChild(journalEntryNode(row, nowMs));
  }
  section.appendChild(list);
  section.appendChild(
    el(
      "p",
      "Showing " +
        String(view.journalStart + 1) +
        "-" +
        String(view.journalStart + view.journal.length) +
        " of " +
        String(total) +
        ".",
    ),
  );
  if (view.history) {
    section.appendChild(el("p", "Browsing history; the newest entries pause here."));
    const latest = document.createElement("button");
    latest.setAttribute("type", "button");
    latest.setAttribute("data-log-latest", "");
    latest.textContent = "back to latest";
    section.appendChild(latest);
  }
  if (view.journalStart > 0 && view.journal.length < LOG_JOURNAL_CAP) {
    const older = document.createElement("button");
    older.setAttribute("type", "button");
    older.setAttribute("data-log-older", "");
    older.textContent = "load older";
    section.appendChild(older);
  } else if (view.journal.length >= LOG_JOURNAL_CAP) {
    section.appendChild(
      el(
        "p",
        "Showing the newest " +
          String(LOG_JOURNAL_CAP) +
          " entries; narrow the kind filter to see older ones.",
      ),
    );
  }
  return section;
}

// The log view: one project at a time, its unresolved stack over
// its logbook. "All" shows the project picker, never a merged log.
export function renderLog(view, { nowMs, notice }) {
  renderTabs("log");
  renderChips(view.choices, view.project, "#/log");
  const note = document.getElementById("notice");
  note.textContent = "";
  if (notice) {
    note.hidden = false;
    note.appendChild(document.createTextNode(cleanText(notice)));
  } else {
    note.hidden = true;
  }
  const cards = document.getElementById("cards");
  cards.textContent = "";
  if (view.derivedOnly) {
    cards.appendChild(el("p", "Project list is derived from activity only."));
  }
  if (view.project === null) {
    const section = card("log");
    section.appendChild(el("p", "Choose a project to see its log."));
    cards.appendChild(section);
  } else if (view.unknown) {
    const section = card("log");
    section.appendChild(el("p", "Unknown project " + String(view.project) + "."));
    cards.appendChild(section);
  } else {
    cards.appendChild(eventsCard(view, nowMs));
    cards.appendChild(journalCard(view, nowMs));
  }
  document.getElementById("agents").textContent = "";
}

// A fatal gate: the API major moved or the document schema is too
// old. Data stops rendering until the operator upgrades; the last
// good cards are cleared so old numbers never pose as current.
export function renderStopped(message) {
  const nav = document.getElementById("chips");
  nav.textContent = "";
  const note = document.getElementById("notice");
  note.textContent = "";
  note.hidden = false;
  note.appendChild(document.createTextNode(cleanText(message)));
  document.getElementById("cards").textContent = "";
  document.getElementById("agents").textContent = "";
}
