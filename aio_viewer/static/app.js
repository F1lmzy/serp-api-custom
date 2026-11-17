"use strict";

const $ = (id) => document.getElementById(id);
const state = { runs: [], run: null, runOffset: 0, offset: 0, search: "", filters: {}, row: null, references: [], listRequest: 0, detailRequest: 0 };
const PAGE_SIZE = 50;
const array = (value) => Array.isArray(value) ? value : [];
const text = (value) => typeof value === "string" || typeof value === "number" ? String(value) : "";

function element(tag, className, content) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content !== undefined) node.textContent = text(content);
  return node;
}
function safeURL(value) {
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}
function link(label, url, className) {
  const href = safeURL(url);
  const node = element(href ? "a" : "span", className, label);
  if (href) { node.href = href; node.target = "_blank"; node.rel = "noopener noreferrer"; }
  return node;
}
function domain(url) {
  try { return new URL(url).hostname.replace(/^www\./, ""); } catch { return "Unknown source"; }
}
function date(value) {
  if (!value) return "Capture time unknown";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? text(value) : parsed.toLocaleString();
}
async function api(path, params = {}) {
  let response;
  try {
    response = await fetch(path + "?" + new URLSearchParams(params), { cache: "no-store", credentials: "same-origin", redirect: "error" });
  } catch {
    throw new Error("Could not reach the viewer. Check your connection and try again.");
  }
  if (!response.headers.get("Content-Type")?.includes("application/json")) throw new Error("The viewer returned an unexpected response. Refresh the page and try again.");
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Could not load saved responses.");
  return data;
}
function notice(message, error = false) {
  $("notice").hidden = !message;
  $("notice").textContent = message;
  $("notice").classList.toggle("error", error);
}
function switchTab(name) {
  for (const tab of ["preview", "json"]) {
    const active = tab === name;
    $(tab + "-tab").setAttribute("aria-selected", String(active));
    $(tab + "-tab").tabIndex = active ? 0 : -1;
  }
  $("preview").hidden = name !== "preview";
  $("json-panel").hidden = name !== "json";
}
function showJSON(scope) {
  $("json-scope").value = scope;
  renderJSON();
  switchTab("json");
  $("json-tab").focus();
}
function renderJSON() {
  if (!state.row) return;
  const scope = $("json-scope").value;
  $("json-content").textContent = JSON.stringify(scope === "response" ? state.row : state.row[scope], null, 2);
  $("copy-status").textContent = "";
}

// Build all captured content as text nodes. Stored HTML is never executed.
function appendSnippet(parent, node) {
  const snippet = text(node.snippet ?? node.text);
  const links = array(node.snippet_links).filter((item) => item && text(item.text) && safeURL(item.link));
  let cursor = 0;
  const ranges = [];
  for (const item of links) {
    const start = snippet.indexOf(text(item.text));
    if (start >= 0) ranges.push({ start, end: start + text(item.text).length, item });
  }
  ranges.sort((a, b) => a.start - b.start);
  for (const range of ranges) {
    if (range.start < cursor) continue;
    parent.append(document.createTextNode(snippet.slice(cursor, range.start)), link(range.item.text, range.item.link));
    cursor = range.end;
  }
  parent.append(document.createTextNode(snippet.slice(cursor)));
}
function showSources(references, indexes = null) {
  const dialog = $("sources-dialog");
  $("sources-dialog-content").replaceChildren();
  $("sources-dialog-title").textContent = indexes ? `Sources ${indexes.join(", ")}` : `Saved sources · ${references.length}`;
  $("sources-dialog-note").textContent = "Original saved order and citation indexes. Images and emphasis were not retained.";
  const selected = indexes ? references.filter((ref) => indexes.includes(ref.index)) : references;
  selected.forEach((ref, ordinal) => $("sources-dialog-content").append(sourceCard(ref, ordinal, true)));
  if (!selected.length) $("sources-dialog-content").append(element("p", "state-message", "No mapped sources were saved."));
  if (!dialog.open) dialog.showModal();
}
function appendCitations(parent, node, references) {
  const indexes = array(node.reference_indexes);
  if (indexes.length) parent.dataset.citationIndexes = JSON.stringify(indexes);
  // A reference index is not an array offset. Zero, sparse and duplicate
  // observed indexes retain their original meaning; never guess a mapping.
  const mapped = indexes.filter((index) => references.some((ref) => ref.index === index));
  if (mapped.length) {
    const citation = element("button", "citation");
    citation.type = "button";
    citation.dataset.referenceIndexes = JSON.stringify(mapped);
    citation.setAttribute("aria-label", `Open saved sources ${mapped.join(", ")}`);
    citation.setAttribute("aria-haspopup", "dialog");
    citation.setAttribute("aria-controls", "sources-dialog");
    citation.title = `Saved citation indexes: ${mapped.join(", ")}`;
    citation.addEventListener("click", () => showSources(references, mapped));
    parent.append(citation);
  }
  for (const index of indexes.filter((value) => !references.some((ref) => ref.index === value))) {
    const missing = element("span", "citation unmapped", `? ${index}`);
    missing.dataset.referenceIndex = text(index);
    missing.title = `Unmapped citation index ${index}; no source was saved for this index`;
    missing.setAttribute("aria-label", missing.title);
    parent.append(missing);
  }
}
function renderBlock(node, references, depth = 0) {
  if (typeof node === "string") return element("p", "", node);
  if (!node || typeof node !== "object" || depth > 30) return document.createTextNode("");
  const container = element("div", "answer-block");
  // Only explicitly retained titles are emphasized. A colon in a plain-text
  // snippet is not enough evidence to invent a bold range.
  if (text(node.title)) container.append(element("strong", "answer-block-title", node.title));
  if (node.snippet || node.text) {
    const paragraph = element(node.type === "heading" ? "h3" : "p");
    if (node.link && !node.snippet) paragraph.append(link(node.text, node.link));
    else appendSnippet(paragraph, node);
    appendCitations(paragraph, node, references);
    container.append(paragraph);
  }
  const items = array(node.list ?? node.items);
  if (items.length) {
    const descriptive = !node.ordered && items.every((item) => item && text(item.title));
    const list = element(node.ordered ? "ol" : "ul", descriptive ? "descriptive-list" : "");
    if (node.ordered && Number.isInteger(node.start)) list.start = node.start;
    for (const item of items) {
      const li = element("li");
      li.append(renderBlock(item, references, depth + 1));
      list.append(li);
    }
    container.append(list);
  }
  const rows = array(node.table ?? (node.type === "table" ? node.detailed : null));
  if (rows.length) {
    const scroll = element("div", "table-scroll");
    const table = element("table");
    for (const row of rows) {
      const tr = element("tr");
      for (const cell of array(row)) {
        const td = element(cell?.is_header ? "th" : "td");
        for (const name of ["colspan", "rowspan"]) {
          if (Number.isInteger(cell?.[name]) && cell[name] > 0 && cell[name] <= 100) td.setAttribute(name, cell[name]);
        }
        td.append(renderBlock(cell, references, depth + 1));
        tr.append(td);
      }
      table.append(tr);
    }
    scroll.append(table);
    container.append(scroll);
  }
  if (!node.snippet && !node.text) appendCitations(container, node, references);
  return container;
}
function sourceCard(ref, ordinal, inDialog = false) {
  const card = element("article", "source-card");
  if (!inDialog) card.id = ref._id;
  card.dataset.referenceIndex = ref.index == null ? "" : text(ref.index);
  if (inDialog) card.append(element("p", "source-index", ref.index == null ? "No observed citation index" : `Citation index ${ref.index}`));
  card.append(link(ref.title || ref.link || `Source ${ordinal + 1}`, ref.link, "source-title"));
  if (ref.snippet) card.append(element("p", "source-snippet", ref.snippet));
  card.append(element("div", "source-name", ref.source || domain(ref.link)));
  return card;
}
function richText(value, depth = 0) {
  if (depth > 8 || value == null) return "";
  if (typeof value !== "object") return text(value);
  return Object.entries(value).filter(([key]) => !["link", "thumbnail", "image"].includes(key))
    .map(([, item]) => richText(item, depth + 1)).filter(Boolean).join(" · ");
}
function organicResult(result, ordinal) {
  const article = element("article", "search-result");
  const identity = element("div", "result-identity");
  const site = element("div");
  site.append(element("div", "site-name", result.source || domain(result.link)), element("div", "display-url", result.displayed_link || result.link));
  identity.append(site);
  article.append(identity);
  const heading = element("h3");
  heading.append(link(result.title || result.link || `Result ${ordinal + 1}`, result.link));
  article.append(heading);
  const topRich = richText(result.rich_snippet?.top);
  if (topRich) article.append(element("p", "rich-snippet", topRich));
  if (result.snippet) {
    const snippet = element("p", "result-snippet");
    if (result.date) snippet.append(element("span", "result-date", text(result.date) + " — "));
    snippet.append(document.createTextNode(text(result.snippet)));
    article.append(snippet);
  }
  if (result.author) article.append(element("p", "result-author", `By ${result.author}`));
  const rich = richText(result.rich_snippet?.bottom ?? (result.rich_snippet?.top ? null : result.rich_snippet));
  if (rich) article.append(element("p", "rich-snippet", rich));
  const inline = element("div", "sitelinks-inline");
  const expanded = element("div", "sitelinks-expanded");
  const values = result.sitelinks;
  for (const item of (Array.isArray(values) ? values : array(values?.inline))) {
    if (item) { const wrapper = element("span"); wrapper.append(link(item.title || item.link, item.link)); inline.append(wrapper); }
  }
  for (const item of array(values?.expanded)) {
    if (!item) continue;
    const entry = element("div", "sitelink-expanded");
    entry.append(link(item.title || item.link, item.link));
    if (item.snippet) entry.append(element("p", "", item.snippet));
    expanded.append(entry);
  }
  if (inline.childElementCount) article.append(inline);
  if (expanded.childElementCount) article.append(expanded);
  return article;
}
function renderResponse(row) {
  closeComparison(false);
  state.row = row;
  $("sources-dialog").close();
  $("welcome").hidden = true;
  $("response").hidden = false;
  $("query-title").textContent = row.query;
  $("metadata").replaceChildren();
  for (const [index, value] of [state.run?.provider, row.query_id, date(row.captured_at), [row.hl, row.gl].filter(Boolean).join(" / "), `Capture: ${row.capture_status || row.status || "unknown"}`].entries()) {
    if (value) $("metadata").append(element("span", index === 1 ? "metadata-item metadata-id" : "metadata-item", value));
  }
  const aio = row.aio_response;
  const refs = array(aio?.references).filter((ref) => ref && typeof ref === "object").map((ref, i) => ({ ...ref, _id: `source-${i}` }));
  const blocks = array(aio?.blocks);
  const status = row.aio_status || "unknown";
  const messages = { absent: "No AI Overview was returned for this saved search.", unavailable: "AI Overview was unavailable for this capture.", empty: "The capture returned an empty AI Overview.", unresolved: "AI Overview extraction was unresolved. Any saved content below may be incomplete.", invalid: "The saved AI Overview was marked invalid.", error: "An error occurred while capturing the AI Overview." };
  $("aio-state").replaceChildren();
  if (status !== "present") $("aio-state").append(element("p", "state-message", `AIO ${status}: ${messages[status] || "AI Overview status is unknown."}`));
  if (row.error_message) $("aio-state").append(element("p", "state-message", row.error_message));
  if (array(row.warnings).length) $("aio-state").append(element("p", "state-message", `Capture warnings: ${row.warnings.join(" · ")}`));
  $("aio-content").replaceChildren();
  for (const block of blocks) $("aio-content").append(renderBlock(block, refs));
  if (!blocks.length && (aio?.text || aio?.paragraph)) $("aio-content").append(element("p", "preserve-lines", aio.text || aio.paragraph));
  if (status === "present" && !blocks.length && !aio?.text && !aio?.paragraph) $("aio-content").append(element("p", "state-message", "This row is marked present but has no readable saved answer."));
  state.references = refs;
  $("sources").replaceChildren();
  $("mobile-sources").hidden = !refs.length;
  $("mobile-sources").textContent = `Sources · ${refs.length}`;
  if (refs.length) {
    const sourceList = element("div", "source-list");
    refs.forEach((ref, index) => { const card = sourceCard(ref, index); card.hidden = index >= 3; sourceList.append(card); });
    const toggle = element("button", "sources-toggle", `Show all ${refs.length} sources`);
    toggle.type = "button";
    toggle.setAttribute("aria-expanded", "false");
    toggle.addEventListener("click", () => {
      if (refs.length <= 3) { showSources(refs); return; }
      const expanded = toggle.getAttribute("aria-expanded") !== "true";
      Array.from(sourceList.children).forEach((card, index) => { card.hidden = !expanded && index >= 3; });
      toggle.setAttribute("aria-expanded", String(expanded));
      toggle.textContent = expanded ? "Show fewer sources" : `Show all ${refs.length} sources`;
    });
    $("sources").append(sourceList, toggle);
  }
  const results = array(row.organic_results);
  $("organic-results").replaceChildren();
  results.forEach((result, index) => { if (result) $("organic-results").append(organicResult(result, index)); });
  if (!results.length) $("organic-results").append(element("p", "muted", "No organic results were saved for this response."));
  $("organic-title").textContent = `Search results · ${results.length}`;
  $("json-scope").value = "response";
  renderJSON();
  switchTab("preview");
}
async function selectRecord(row, button) {
  closeComparison(false);
  const request = ++state.detailRequest;
  const runID = state.run.run_id;
  document.querySelectorAll(".record").forEach((node) => node.setAttribute("aria-pressed", String(node === button)));
  $("response").hidden = true;
  $("welcome").hidden = true;
  state.row = null;
  $("sources-dialog").close();
  $("main").setAttribute("aria-busy", "true");
  notice("Loading saved response…");
  try {
    const data = await api("/api/response", { run_id: runID, query_id: row.query_id });
    if (request !== state.detailRequest) return;
    renderResponse(data);
    if (window.matchMedia("(max-width: 999px)").matches) setArchiveOpen(false);
    notice("");
  } catch (error) { if (request === state.detailRequest) notice(error.message, true); }
  finally { if (request === state.detailRequest) $("main").setAttribute("aria-busy", "false"); }
}
async function loadQueries() {
  closeComparison(false);
  const request = ++state.listRequest;
  ++state.detailRequest;
  state.row = null;
  $("sources-dialog").close();
  $("response").hidden = true;
  $("welcome").hidden = false;
  $("main").setAttribute("aria-busy", "false");
  notice("");
  $("records").replaceChildren();
  $("prev").disabled = $("next").disabled = true;
  $("list-status").textContent = "Loading responses…";
  $("page").textContent = "";
  if (!state.run) { $("list-status").textContent = "No capture runs found."; return; }
  try {
    const data = await api("/api/queries", { run_id: state.run.run_id, offset: state.offset, search: state.search, limit: PAGE_SIZE, ...state.filters });
    if (request !== state.listRequest) return;
    for (const row of data.items) {
      const button = element("button", "record");
      button.type = "button";
      button.setAttribute("aria-pressed", "false");
      const top = element("div", "record-top");
      top.append(element("span", "record-id", row.query_id), element("span", "badge", row.aio_status || "unknown"));
      button.append(top, element("span", "record-query", row.query));
      button.addEventListener("click", () => selectRecord(row, button));
      $("records").append(button);
    }
    $("list-status").textContent = data.items.length ? `${state.offset + 1}–${state.offset + data.items.length} responses${state.search || Object.keys(state.filters).length ? " matching your filters" : ""}` : "No matching responses.";
    $("prev").disabled = state.offset === 0;
    $("next").disabled = !data.has_more;
    $("page").textContent = `Page ${state.offset / PAGE_SIZE + 1}`;
  } catch (error) { if (request === state.listRequest) { $("list-status").textContent = "Could not load responses."; notice(error.message, true); } }
}
async function loadRuns() {
  closeComparison(false);
  $("runs-prev").disabled = $("runs-next").disabled = true;
  $("run").disabled = true;
  $("prev").disabled = $("next").disabled = true;
  state.run = null;
  state.row = null;
  ++state.listRequest;
  ++state.detailRequest;
  $("records").replaceChildren();
  $("response").hidden = true;
  $("welcome").hidden = false;
  notice("Loading capture runs…");
  try {
    const data = await api("/api/runs", { offset: state.runOffset, limit: PAGE_SIZE });
    state.runs = data.items;
    $("run").replaceChildren();
    for (const run of state.runs) {
      const option = element("option", "", `${run.provider || "custom"} · ${run.label || run.run_id} · round ${run.run_index || 1} · ${run.run_id.slice(0, 8)}`);
      option.value = run.run_id;
      $("run").append(option);
    }
    if (!state.runs.length) $("run").append(element("option", "", "No stored runs"));
    $("runs-prev").disabled = state.runOffset === 0;
    $("runs-next").disabled = !data.has_more;
    $("run-page").textContent = `Runs · page ${state.runOffset / PAGE_SIZE + 1}`;
    state.run = state.runs[0] || null;
    state.offset = 0;
    await loadQueries();
  } catch (error) { notice(error.message, true); $("runs-prev").disabled = state.runOffset === 0; $("runs-next").disabled = false; }
  finally { $("run").disabled = false; }
}

// Comparison uses the same safe renderers, but independent request generations:
// stale run/match/detail responses cannot replace a newer comparison or query.
const comparison = { request: 0, runRequest: 0, runOffset: 0, matchOffset: 0, base: null, baseRun: null, runs: [], matches: [] };
function resetComparisonResults() {
  ++comparison.request;
  comparison.matches = [];
  $("compare-match-controls").hidden = true;
  $("compare-summary").hidden = true;
  $("compare-summary").replaceChildren();
  $("compare-panes").replaceChildren();
}
function closeComparison(reveal = true) {
  ++comparison.runRequest;
  resetComparisonResults();
  $("comparison").hidden = true;
  $("sources-dialog").close();
  if (reveal && state.row) { $("response").hidden = false; $("compare-open").focus(); }
}
function runLabel(run) {
  return `${run.provider || "custom"} · ${run.label || run.run_id} · round ${run.run_index || 1} · ${run.run_id.slice(0, 8)}${run.created_at ? " · " + date(run.created_at) : ""}`;
}
async function loadComparisonRuns() {
  const request = ++comparison.runRequest;
  resetComparisonResults();
  $("compare-run").replaceChildren();
  $("compare-load").disabled = true;
  $("compare-runs-prev").disabled = $("compare-runs-next").disabled = true;
  $("compare-status").textContent = "Loading comparison runs…";
  try {
    const data = await api("/api/runs", { offset: comparison.runOffset, limit: PAGE_SIZE });
    if (request !== comparison.runRequest) return;
    comparison.runs = data.items.filter((run) => run.run_id !== comparison.base.run_id);
    for (const run of comparison.runs) {
      const option = element("option", "", runLabel(run)); option.value = run.run_id; $("compare-run").append(option);
    }
    $("compare-load").disabled = !comparison.runs.length;
    $("compare-runs-prev").disabled = comparison.runOffset === 0;
    $("compare-runs-next").disabled = !data.has_more;
    $("compare-run-page").textContent = `Runs · page ${comparison.runOffset / PAGE_SIZE + 1}`;
    $("compare-status").textContent = comparison.runs.length ? "Choose another run, then find its matching query." : "No other runs on this page. Browse older/newer runs or capture another run first.";
  } catch (error) {
    if (request !== comparison.runRequest) return;
    $("compare-status").textContent = error.message;
    $("compare-runs-prev").disabled = comparison.runOffset === 0;
    $("compare-runs-next").disabled = false;
  }
}
async function loadComparisonMatches() {
  resetComparisonResults();
  const request = comparison.request;
  comparison.targetRun = comparison.runs.find((run) => run.run_id === $("compare-run").value);
  if (!comparison.targetRun) return;
  $("compare-status").textContent = "Finding exact query and locale matches…";
  try {
    const data = await api("/api/comparison-matches", { run_id: comparison.base.run_id, query_id: comparison.base.query_id, compare_run_id: comparison.targetRun.run_id, offset: comparison.matchOffset, limit: PAGE_SIZE });
    if (request !== comparison.request) return;
    comparison.matches = data.items;
    $("compare-match").replaceChildren(element("option", "", "Choose a matching capture…"));
    $("compare-match").firstChild.value = "";
    data.items.forEach((row, index) => {
      const label = `${row.query_id} · ${row.aio_status || "unknown"} · ${row.device || "device unknown"} · ${date(row.captured_at)}`;
      const option = element("option", "", label); option.value = String(index); $("compare-match").append(option);
    });
    $("compare-match-controls").hidden = !data.items.length && !comparison.matchOffset;
    $("compare-matches-prev").disabled = comparison.matchOffset === 0;
    $("compare-matches-next").disabled = !data.has_more;
    $("compare-match-page").textContent = `Matches · page ${comparison.matchOffset / PAGE_SIZE + 1}`;
    $("compare-status").textContent = data.items.length ? "Multiple matches are never paired by position or query ID. Choose the capture to compare." : "No exact query + language/country match in this run. Missing captures are not treated as AIO absent.";
    if (data.items.length === 1 && !data.has_more && comparison.matchOffset === 0) {
      $("compare-match").value = "0";
      await selectComparisonMatch();
    }
  } catch (error) { if (request === comparison.request) $("compare-status").textContent = error.message; }
}
function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === "object") return Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonical(value[key])]));
  return value;
}
function sameJSON(a, b) { return JSON.stringify(canonical(a)) === JSON.stringify(canonical(b)); }
function comparisonPane(row, run, side) {
  const pane = element("article", "comparison-pane"); pane.id = `compare-side-${side.toLowerCase()}`;
  pane.append(element("h2", "", `Run ${side} · ${run.label || run.run_id}`));
  pane.append(element("p", "comparison-pane-meta", `${run.provider || "custom"} · round ${run.run_index || 1} · run ${run.run_id.slice(0, 8)} · ${row.query_id} · ${date(row.captured_at)} · ${[row.hl, row.gl].filter(Boolean).join(" / ") || "Locale unknown"} · ${row.device || "Device unknown"} · ${row.google_domain || "Domain unknown"}`));
  pane.append(element("p", "comparison-pane-status", `AIO: ${row.aio_status || "unknown"} · Capture: ${row.capture_status || row.status || "unknown"}`));
  if (row.error_message) pane.append(element("p", "state-message", row.error_message));
  if (array(row.warnings).length) pane.append(element("p", "state-message", `Warnings: ${row.warnings.join(" · ")}`));
  pane.append(element("h3", "comparison-section-title", "AI Overview"));
  const answer = element("div", "aio-content");
  const refs = array(row.aio_response?.references).filter((ref) => ref && typeof ref === "object");
  const blocks = array(row.aio_response?.blocks);
  blocks.forEach((block) => answer.append(renderBlock(block, refs)));
  if (!blocks.length) answer.append(element("p", "preserve-lines", row.aio_response?.text || row.aio_response?.paragraph || `No readable saved AIO content (status: ${row.aio_status || "unknown"}).`));
  pane.append(answer);
  const sources = element("details", "comparison-sources"); sources.append(element("summary", "", `Saved AIO sources · ${refs.length}`));
  refs.forEach((ref, index) => sources.append(sourceCard(ref, index, true))); pane.append(sources);
  pane.append(element("h3", "comparison-section-title", `Organic results · ${array(row.organic_results).length}`));
  const results = element("div", "comparison-organic");
  array(row.organic_results).forEach((result, index) => { if (result) results.append(organicResult(result, index)); });
  if (!results.childElementCount) results.append(element("p", "state-message", "No organic results were saved."));
  pane.append(results);
  const raw = element("details", "comparison-json"); raw.append(element("summary", "", `Raw JSON · Run ${side}`), element("pre", "", JSON.stringify(row, null, 2))); pane.append(raw);
  return pane;
}
function renderComparisonSummary(left, right) {
  const summary = $("compare-summary"); summary.replaceChildren(); summary.hidden = false;
  summary.append(element("h2", "", "What changed"));
  const metrics = element("ul", "comparison-metrics");
  const leftAIO = left.aio_response, rightAIO = right.aio_response;
  for (const line of [
    `AIO status: ${left.aio_status || "unknown"} → ${right.aio_status || "unknown"}`,
    `AIO JSON: ${sameJSON(leftAIO, rightAIO) ? "identical" : "changed"}`,
    `AIO sources: ${array(leftAIO?.references).length} → ${array(rightAIO?.references).length}`,
    `Organic results: ${array(left.organic_results).length} → ${array(right.organic_results).length}`,
    `Organic JSON: ${sameJSON(left.organic_results, right.organic_results) ? "identical" : "changed"}`,
  ]) metrics.append(element("li", "", line));
  summary.append(metrics);
  const groups = (row) => {
    const map = new Map();
    array(row.organic_results).forEach((result, index) => {
      if (!result || !text(result.link)) return;
      if (!map.has(result.link)) map.set(result.link, []);
      map.get(result.link).push(result.position ?? `order ${index + 1}`);
    });
    return map;
  };
  const a = groups(left), b = groups(right), urls = Array.from(new Set([...a.keys(), ...b.keys()]));
  const changes = urls.map((url) => ({ url, before: a.get(url), after: b.get(url), change: !a.has(url) ? "Added" : !b.has(url) ? "Removed" : sameJSON(a.get(url), b.get(url)) ? "Same positions" : "Moved" }));
  const details = element("details", "comparison-ranks");
  details.append(element("summary", "", `Organic URL changes · ${changes.filter((r) => r.change === "Added").length} added · ${changes.filter((r) => r.change === "Removed").length} removed · ${changes.filter((r) => r.change === "Moved").length} moved`));
  details.append(element("p", "muted", "Exact saved URLs; tracking parameters are not normalized. Repeated URLs keep all positions. ‘order’ means no position was saved. Results without URLs remain in the full panes below."));
  const scroll = element("div", "table-scroll"), table = element("table"), head = element("tr");
  for (const name of ["Saved URL", "Run A position", "Run B position", "Change"]) head.append(element("th", "", name));
  const thead = element("thead"); thead.append(head); table.append(thead);
  const tbody = element("tbody");
  changes.forEach((item) => {
    const tr = element("tr"), url = element("td"); url.append(link(item.url, item.url));
    tr.append(url, element("td", "", item.before?.join(", ") || "—"), element("td", "", item.after?.join(", ") || "—"), element("td", "", item.change)); tbody.append(tr);
  });
  table.append(tbody); scroll.append(table); details.append(scroll); summary.append(details);
  const jumps = element("p", "comparison-jumps");
  for (const side of ["A", "B"]) { const jump = element("a", "", `Jump to Run ${side}`); jump.href = `#compare-side-${side.toLowerCase()}`; jumps.append(jump); }
  summary.append(jumps);
}
async function selectComparisonMatch() {
  const request = ++comparison.request;
  $("compare-panes").replaceChildren(); $("compare-summary").hidden = true;
  if ($("compare-match").value === "") { $("compare-status").textContent = "Choose a matching capture to compare."; return; }
  const match = comparison.matches[Number($("compare-match").value)];
  if (!match) return;
  $("compare-status").textContent = "Loading matching saved response…";
  try {
    const right = await api("/api/response", { run_id: comparison.targetRun.run_id, query_id: match.query_id });
    if (request !== comparison.request) return;
    const left = comparison.base;
    if (right.query !== left.query || (right.hl ?? null) !== (left.hl ?? null) || (right.gl ?? null) !== (left.gl ?? null)) throw new Error("The saved query changed while comparing. Reload the selected response and try again.");
    renderComparisonSummary(left, right);
    $("compare-panes").append(comparisonPane(left, comparison.baseRun, "A"), comparisonPane(right, comparison.targetRun, "B"));
    $("compare-status").textContent = "Comparing exact query text and language/country. Full saved content is preserved below; JSON checks ignore object-key order, not array order.";
  } catch (error) { if (request === comparison.request) $("compare-status").textContent = error.message; }
}
$("compare-open").addEventListener("click", () => {
  if (!state.row) return;
  comparison.base = state.row; comparison.baseRun = state.run;
  comparison.runOffset = comparison.matchOffset = 0;
  $("response").hidden = true; $("comparison").hidden = false;
  $("comparison-query").textContent = state.row.query;
  $("compare-close").focus();
  loadComparisonRuns();
});
$("compare-close").addEventListener("click", () => closeComparison());
$("compare-load").addEventListener("click", () => { comparison.matchOffset = 0; loadComparisonMatches(); });
$("compare-run").addEventListener("change", () => { resetComparisonResults(); comparison.matchOffset = 0; $("compare-status").textContent = "Find matching query to compare this run."; });
$("compare-match").addEventListener("change", selectComparisonMatch);
$("compare-runs-prev").addEventListener("click", () => { comparison.runOffset = Math.max(0, comparison.runOffset - PAGE_SIZE); loadComparisonRuns(); });
$("compare-runs-next").addEventListener("click", () => { comparison.runOffset += PAGE_SIZE; loadComparisonRuns(); });
$("compare-matches-prev").addEventListener("click", () => { comparison.matchOffset = Math.max(0, comparison.matchOffset - PAGE_SIZE); loadComparisonMatches(); });
$("compare-matches-next").addEventListener("click", () => { comparison.matchOffset += PAGE_SIZE; loadComparisonMatches(); });

$("run").addEventListener("change", () => { state.run = state.runs.find((run) => run.run_id === $("run").value); state.offset = 0; loadQueries(); });
function applyFilters() {
  state.search = $("search").value.trim();
  state.filters = Object.fromEntries(Array.from(new FormData($("search-form"))).filter(([, value]) => value.trim()).map(([key, value]) => [key, value.trim()]));
  state.offset = 0;
  loadQueries();
}
$("search-form").addEventListener("submit", (event) => { event.preventDefault(); applyFilters(); });
$("search-form").addEventListener("change", (event) => { if (event.target.matches("select")) applyFilters(); });
$("filters-clear").addEventListener("click", () => { $("search-form").reset(); applyFilters(); });
$("prev").addEventListener("click", () => { state.offset = Math.max(0, state.offset - PAGE_SIZE); loadQueries(); });
$("next").addEventListener("click", () => { state.offset += PAGE_SIZE; loadQueries(); });
$("runs-prev").addEventListener("click", () => { state.runOffset = Math.max(0, state.runOffset - PAGE_SIZE); loadRuns(); });
$("runs-next").addEventListener("click", () => { state.runOffset += PAGE_SIZE; loadRuns(); });
$("preview-tab").addEventListener("click", () => switchTab("preview"));
$("json-tab").addEventListener("click", () => switchTab("json"));
for (const id of ["preview-tab", "json-tab"]) $(id).addEventListener("keydown", (event) => {
  if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
    event.preventDefault();
    const target = event.key === "Home" ? "preview" : event.key === "End" ? "json" : id === "preview-tab" ? "json" : "preview";
    switchTab(target);
    $(target + "-tab").focus();
  }
});
$("aio-json").addEventListener("click", () => showJSON("aio_response"));
$("organic-json").addEventListener("click", () => showJSON("organic_results"));
$("json-scope").addEventListener("change", renderJSON);
$("copy-json").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText($("json-content").textContent); $("copy-status").textContent = "JSON copied"; }
  catch { $("copy-status").textContent = "Clipboard unavailable. Select the JSON text to copy it manually."; }
});
const archiveBreakpoint = window.matchMedia("(max-width: 999px)");
function setArchiveOpen(open) {
  document.body.classList.toggle("archive-open", open);
  if (!open && $("archive-sidebar").contains(document.activeElement)) $("main").focus({ preventScroll: true });
  $("archive-sidebar").hidden = !open;
  $("archive-toggle").setAttribute("aria-expanded", String(open));
  $("archive-toggle").textContent = open ? "Hide archive" : "Browse archive";
  $("archive-backdrop").hidden = !open || !archiveBreakpoint.matches;
}
$("archive-toggle").addEventListener("click", () => setArchiveOpen(!document.body.classList.contains("archive-open")));
$("archive-backdrop").addEventListener("click", () => setArchiveOpen(false));
archiveBreakpoint.addEventListener("change", () => setArchiveOpen(!archiveBreakpoint.matches));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && archiveBreakpoint.matches && document.body.classList.contains("archive-open")) {
    setArchiveOpen(false); $("archive-toggle").focus();
  }
});
$("sources-close").addEventListener("click", () => $("sources-dialog").close());
$("mobile-sources").addEventListener("click", () => showSources(state.references));
setArchiveOpen(!archiveBreakpoint.matches);
loadRuns();
