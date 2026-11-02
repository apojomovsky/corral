// View renderers. Untrusted strings enter the DOM as text only,
// never as markup; this module sets textContent and nothing else.
// Bar geometry rides on SVG attributes, never CSS: the policy
// allows no inline style, so a width can only be an attribute.

import { agentKey, detailHash, hasTicket, isErrorSection, snapshotProjects } from "./api.js";
import {
  cleanText,
  formatAge,
  formatBytes,
  formatClock,
  formatPct,
  parseEpoch,
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
      const key = agentKey(row);
      if (key === null) {
        item.appendChild(el("span", shown.line));
      } else {
        const link = document.createElement("a");
        link.setAttribute("href", detailHash(key));
        link.textContent = cleanText(shown.line);
        item.appendChild(link);
      }
      if (shown.note !== null && shown.note !== undefined && String(shown.note) !== "") {
        item.appendChild(el("span", "  note: " + String(shown.note)));
      }
      list.appendChild(item);
    }
    section.appendChild(list);
  }
  return section;
}

function chipsNodes(projects, selected) {
  const nodes = [];
  const all = document.createElement("a");
  all.setAttribute("href", "#/");
  all.textContent = "All";
  if (selected === null) {
    all.setAttribute("aria-current", "true");
  }
  nodes.push(all);
  for (const name of projects) {
    const chip = document.createElement("a");
    chip.setAttribute("href", "#/?project=" + encodeURIComponent(name));
    chip.textContent = cleanText(name);
    if (name === selected) {
      chip.setAttribute("aria-current", "true");
    }
    nodes.push(chip);
  }
  return nodes;
}

export function renderChips(projects, selected) {
  document.getElementById("chips").replaceChildren(...chipsNodes(projects, selected));
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
// grouped by project under the selected filter. Every node builds
// off-DOM and the live sections swap once, so a throw below leaves
// the last good page in place for the poll loop's catch instead of
// a half-drawn update.
export function renderOverview(doc, { project, nowMs, notice }) {
  const chips = chipsNodes(snapshotProjects(doc), project);
  const cards = [
    headerLine(doc.fleet_header),
    quotaCard(doc.quota, nowMs),
    capacityCard(doc.capacity),
    resourcesCard(doc.resources),
    diskCard(doc.disk),
    driveCard(doc.drive, nowMs),
  ];
  const agents = agentsSection(doc.agents, project, nowMs);
  document.getElementById("detail").hidden = true;
  const nav = document.getElementById("chips");
  nav.hidden = false;
  nav.replaceChildren(...chips);
  const note = document.getElementById("notice");
  note.textContent = notice ? cleanText(notice) : "";
  note.hidden = !notice;
  const cardsNode = document.getElementById("cards");
  cardsNode.hidden = false;
  cardsNode.replaceChildren(...cards);
  const agentsNode = document.getElementById("agents");
  agentsNode.hidden = false;
  agentsNode.replaceChildren(agents);
}

function ticketTitle(row) {
  if (!hasTicket(row)) {
    return "no ticket";
  }
  return String(row.repo) + "#" + String(row.number);
}

function fieldValue(value) {
  return value === null || value === undefined || value === "" ? "not reported" : String(value);
}

// The agent detail: every row field, then the tail pane for rows
// with a ticket. Fields build off-DOM and swap once like the
// overview; the tail pane is owned by the tail poll and stays put
// across field re-renders.
export function renderDetail(row, { nowMs }) {
  const info = row && typeof row === "object" ? row : {};
  const project =
    typeof info.project === "string" && info.project !== "" ? info.project : null;
  const backHref = project === null ? "#/" : "#/?project=" + encodeURIComponent(project);
  const shown = agentText(info, nowMs);
  const lines = [
    el("h2", ticketTitle(info) + "  " + String(info.state)),
    el("p", shown.line),
  ];
  if (shown.note !== null && shown.note !== undefined && String(shown.note) !== "") {
    lines.push(el("p", "note: " + String(shown.note)));
  }
  lines.push(el("p", "id: " + fieldValue(info.id)));
  lines.push(el("p", "project: " + (project === null ? "none" : project)));
  lines.push(el("p", "profile: " + fieldValue(info.profile)));
  const ticketed = hasTicket(info);
  if (!ticketed) {
    lines.push(el("p", "no ticket, no tail"));
  }
  document.getElementById("chips").hidden = true;
  document.getElementById("cards").hidden = true;
  document.getElementById("agents").hidden = true;
  const back = document.getElementById("detail-back");
  back.setAttribute("href", backHref);
  document.getElementById("detail-fields").replaceChildren(...lines);
  document.getElementById("tail-controls").hidden = !ticketed;
  document.getElementById("tail").hidden = !ticketed;
  if (!ticketed) {
    document.getElementById("tail").textContent = "";
    document.getElementById("tail-state").textContent = "";
  }
  document.getElementById("detail").hidden = false;
}

// A detail key that names nothing in the fresh document. States the
// miss in the detail pane; never a guessed neighbour.
export function renderDetailMissing() {
  document.getElementById("chips").hidden = true;
  document.getElementById("cards").hidden = true;
  document.getElementById("agents").hidden = true;
  const back = document.getElementById("detail-back");
  back.setAttribute("href", "#/");
  document
    .getElementById("detail-fields")
    .replaceChildren(el("p", "this agent is no longer listed"));
  document.getElementById("tail-controls").hidden = true;
  document.getElementById("tail").hidden = true;
  document.getElementById("tail").textContent = "";
  document.getElementById("tail-state").textContent = "";
  document.getElementById("detail").hidden = false;
}

// The tail pane is one text node, replaced per poll: memory stays
// flat and no per-line nodes ever exist.
export function renderTail(text) {
  document.getElementById("tail").textContent = cleanText(text === null ? "" : String(text));
  document.getElementById("tail-state").textContent = "";
}

export function renderTailMissing() {
  document.getElementById("tail").textContent = "nothing to show";
  document.getElementById("tail-state").textContent = "";
}

export function renderTailFailed() {
  document.getElementById("tail-state").textContent = "tail unavailable, retrying";
}

export function renderTailLoading() {
  document.getElementById("tail").textContent = "";
  document.getElementById("tail-state").textContent = "loading the tail";
}

export function renderTailPaused(paused) {
  document.getElementById("tail-state").textContent = paused ? "tail paused" : "";
}

export function renderTailButtons(paused, follow) {
  document.getElementById("tail-pause").textContent = paused ? "tail: resume" : "tail: pause";
  document.getElementById("tail-follow").textContent = follow ? "follow: on" : "follow: off";
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
  document.getElementById("detail").hidden = true;
}
