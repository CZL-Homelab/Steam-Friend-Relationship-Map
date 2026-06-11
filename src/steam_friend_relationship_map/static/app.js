const $ = (id) => document.getElementById(id);

const FALLBACK_ZH = {
  "app.title": "Steam 好友关系图谱",
  "app.subtitle": "Neo4j 本地图数据库",
  "graph.summary": "{nodes} 个节点 · {edges} 条关系",
  "graph.summaryLimited": "{nodes} 个节点 · {edges} 条关系 · 已限制",
  "graph.loadFailed": "图谱加载失败",
  "graph.emptyTitle": "暂无图谱",
  "graph.emptyHint": "完成抓取或刷新图谱后会显示节点。",
  "log.empty": "暂无日志",
  "path.empty": "未选择路径",
  "path.noPath": "没有路径",
  "profile.empty": "选择一个节点",
  "profile.steamProfile": "Steam 主页",
  "status.idle": "空闲",
  "status.unknown": "未知",
  "toast.rootRequired": "请输入 Root URL",
  "toast.graphLoadFailed": "图谱加载失败，详情见日志",
};

let cy;
let currentRunId = null;
let pollTimer = null;
let systemLogTimer = null;
let selectedNode = null;
let currentGraph = { nodes: [], edges: [], limited: false };
let i18n = { "zh-CN": FALLBACK_ZH, en: {} };
let currentLang = localStorage.getItem("sfm_lang") || "zh-CN";
let lastEventSeq = 0;
let lastSystemLogSeq = 0;

async function loadI18n() {
  try {
    const response = await fetch("/static/i18n.json");
    if (response.ok) i18n = await response.json();
  } catch {
    i18n = { "zh-CN": FALLBACK_ZH, en: {} };
  }
  if (!i18n[currentLang]) currentLang = "zh-CN";
}

function t(key, params = {}) {
  const table = i18n[currentLang] || i18n["zh-CN"] || FALLBACK_ZH;
  const fallback = i18n["zh-CN"] || FALLBACK_ZH;
  let value = table[key] || fallback[key] || key;
  for (const [name, replacement] of Object.entries(params)) {
    value = value.replaceAll(`{${name}}`, String(replacement));
  }
  return value;
}

function setLanguage(lang) {
  currentLang = i18n[lang] ? lang : "zh-CN";
  localStorage.setItem("sfm_lang", currentLang);
  applyTranslations();
}

function translateLabel(label) {
  const key = label.dataset.i18nLabel;
  const textNode = Array.from(label.childNodes).find((node) => node.nodeType === Node.TEXT_NODE && node.textContent.trim());
  if (key && textNode) textNode.textContent = `\n            ${t(key)}\n            `;
}

function applyTranslations() {
  document.documentElement.lang = currentLang;
  document.title = t("app.title");
  document.querySelectorAll("[data-i18n]").forEach((node) => {
    node.textContent = t(node.dataset.i18n);
  });
  document.querySelectorAll("[data-i18n-title]").forEach((node) => {
    node.setAttribute("title", t(node.dataset.i18nTitle));
  });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((node) => {
    node.setAttribute("placeholder", t(node.dataset.i18nPlaceholder));
  });
  document.querySelectorAll("[data-i18n-label]").forEach(translateLabel);
  document.querySelectorAll(".lang-button").forEach((button) => {
    button.classList.toggle("active", button.dataset.lang === currentLang);
  });
  document.querySelectorAll("[data-status]").forEach((node) => {
    node.textContent = statusText(node.dataset.status);
  });
  updateGraphSummary();
  if (!selectedNode) $("profileUrl").textContent = t("profile.steamProfile");
  if ($("pathResult").dataset.state === "empty") $("pathResult").textContent = t("path.empty");
  if ($("pathResult").dataset.state === "no-path") $("pathResult").textContent = t("path.noPath");
  if (!$("crawlLogs").children.length) $("lastEvent").textContent = t("log.empty");
}

function statusText(status) {
  return t(`status.${status || "unknown"}`);
}

function setStatus(id, status) {
  const node = $(id);
  node.dataset.status = status;
  node.textContent = statusText(status);
}

function toast(message) {
  const box = $("toast");
  box.textContent = message;
  box.classList.add("show");
  setTimeout(() => box.classList.remove("show"), 2600);
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

async function api(path, options = {}) {
  try {
    const response = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      throw new Error(detail.detail || response.statusText);
    }
    return response.json();
  } catch (error) {
    appendSystemLog("error", "api", `${path.split("?")[0]}: ${error.message}`);
    throw error;
  }
}

function appendLog(listId, level, source, message, time = new Date().toISOString()) {
  const list = $(listId);
  const row = document.createElement("div");
  row.className = `log-item log-${level}`;
  row.dataset.level = level;
  row.innerHTML = `<span class="log-meta">${escapeHtml(time)} · ${escapeHtml(source)}</span><span>${escapeHtml(message)}</span>`;
  list.appendChild(row);
  while (list.children.length > 300) list.removeChild(list.firstElementChild);
  list.scrollTop = list.scrollHeight;
}

function appendUiLog(level, stage, message, time = new Date().toISOString()) {
  appendLog("crawlLogs", level, stage, message, time);
  $("lastEvent").textContent = message;
}

function appendSystemLog(level, source, message, time = new Date().toISOString()) {
  appendLog("systemLogs", level, source, message, time);
}

function setProgress(percent) {
  $("crawlProgressBar").style.width = `${Math.max(0, Math.min(100, Number(percent) || 0))}%`;
}

function setFieldError(id, message) {
  const input = $(id);
  input.classList.toggle("field-invalid", Boolean(message));
  let error = input.parentElement.querySelector(".field-error");
  if (!error) {
    error = document.createElement("div");
    error.className = "field-error";
    input.parentElement.appendChild(error);
  }
  error.textContent = message || "";
}

function clearFieldErrors(ids) {
  ids.forEach((id) => setFieldError(id, ""));
}

function numberValue(id, fallback = null) {
  const raw = $(id).value.trim();
  if (raw === "") return fallback;
  return Number(raw);
}

function validateRange(minId, maxId) {
  const min = numberValue(minId);
  const max = numberValue(maxId);
  if (min !== null && max !== null && min > max) {
    setFieldError(maxId, t("validation.minMax"));
    return false;
  }
  return true;
}

async function withButtonState(button, action) {
  const node = typeof button === "string" ? $(button) : button;
  node.disabled = true;
  node.classList.remove("button-success", "button-error");
  node.classList.add("is-loading");
  try {
    const result = await action();
    node.classList.add("button-success");
    setTimeout(() => node.classList.remove("button-success"), 900);
    return result;
  } catch (error) {
    node.classList.add("button-error");
    toast(error.message);
    appendSystemLog("error", "ui", error.message);
    setTimeout(() => node.classList.remove("button-error"), 1200);
    throw error;
  } finally {
    node.disabled = false;
    node.classList.remove("is-loading");
  }
}

function initGraph() {
  cy = cytoscape({
    container: $("graph"),
    elements: [],
    style: [
      {
        selector: "node",
        style: {
          "background-color": "#0f766e",
          "background-image": "data(avatar)",
          "background-fit": "cover",
          "border-color": "#ffffff",
          "border-width": 2,
          label: "data(label)",
          color: "#172026",
          "font-size": 11,
          "text-background-color": "#ffffff",
          "text-background-opacity": 0.9,
          "text-background-padding": 3,
          "text-margin-y": 8,
          width: "mapData(visualSize, 0, 100, 34, 92)",
          height: "mapData(visualSize, 0, 100, 34, 92)",
        },
      },
      {
        selector: "node[status = 'private']",
        style: { "border-color": "#be123c", "border-width": 3 },
      },
      {
        selector: "node.analysis-focus",
        style: { "border-color": "#2563eb", "border-width": 5 },
      },
      {
        selector: "node.analysis-evidence",
        style: { "border-color": "#b45309", "border-width": 4 },
      },
      {
        selector: "edge",
        style: {
          width: "mapData(strength, 1, 20, 1.2, 7)",
          "line-color": "#9aa8b2",
          opacity: 0.68,
          "curve-style": "haystack",
        },
      },
      {
        selector: ":selected",
        style: { "border-color": "#2563eb", "border-width": 4, "line-color": "#2563eb" },
      },
    ],
    layout: { name: "cose", animate: false, padding: 40 },
    wheelSensitivity: 0.18,
  });

  cy.on("tap", "node", (event) => {
    selectedNode = event.target.data().node;
    fillProfile(selectedNode);
  });
}

function metricValue(node, metric) {
  if (metric === "friend_count") return node.friend_count ?? 0;
  if (metric === "prior_pool_links") return node.prior_pool_link_count ?? 0;
  if (metric === "closeness") return node.root_closeness_score ?? 0;
  return node.degree ?? 0;
}

function renderGraph(data) {
  currentGraph = data;
  const sizeBy = $("graphSizeBy").value || "degree";
  const maxMetric = Math.max(1, ...data.nodes.map((node) => metricValue(node, sizeBy)));
  const elements = [
    ...data.nodes.map((node) => ({
      data: {
        id: node.id,
        label: node.label,
        avatar: node.avatar,
        degree: node.degree || 1,
        closeness: node.root_closeness_score || 0,
        visualSize: Math.max(5, Math.min(100, (metricValue(node, sizeBy) / maxMetric) * 100)),
        status: node.friend_list_status,
        node,
      },
    })),
    ...data.edges.map((edge) => ({ data: { id: edge.id, source: edge.source, target: edge.target, strength: Math.max(1, edge.strength || 1) } })),
  ];
  cy.elements().remove();
  cy.add(elements);
  runLayout();
  updateGraphSummary();
  $("graphEmpty").classList.toggle("hidden", data.nodes.length > 0);
  if (!data.nodes.length) {
    $("graphEmpty").querySelector("span").textContent = t("graph.emptyFiltered");
  }
}

function updateGraphSummary() {
  const key = currentGraph.limited ? "graph.summaryLimited" : "graph.summary";
  $("graphSummary").textContent = t(key, {
    nodes: currentGraph.nodes.length,
    edges: currentGraph.edges.length,
  });
}

function runLayout() {
  const bias = $("graphLayoutBias")?.value || "cose";
  if (bias === "closeness") {
    cy.layout({
      name: "concentric",
      animate: "end",
      animationDuration: 320,
      padding: 48,
      concentric: (node) => node.data("closeness") || node.data("degree") || 1,
      levelWidth: () => 12,
    }).run();
    return;
  }
  cy.layout({
    name: "cose",
    animate: "end",
    animationDuration: 320,
    padding: 48,
    nodeRepulsion: 9000,
    idealEdgeLength: 90,
  }).run();
}

function fillProfile(node) {
  selectedNode = node;
  $("profileAvatar").hidden = !node.avatar;
  if (node.avatar) $("profileAvatar").src = node.avatar;
  $("profileName").textContent = node.label || statusText("unknown");
  $("profileUrl").href = node.profile_url || "#";
  $("profileUrl").textContent = node.profile_url || t("profile.steamProfile");
  $("profileSteamId").textContent = node.id || "-";
  $("profileDegree").textContent = node.degree ?? 0;
  $("profileFriendCount").textContent = node.friend_count ?? "-";
  $("profilePriorLinks").textContent = node.prior_pool_link_count ?? 0;
  $("profileCloseness").textContent = node.root_closeness_score ?? 0;
  $("profileStatus").dataset.status = node.friend_list_status || "unknown";
  $("profileStatus").textContent = statusText(node.friend_list_status);
  $("profileCategory").value = node.category || "";
  $("profileTags").value = (node.tags || []).join(", ");
  $("profileNote").value = node.note || "";
  $("pathFrom").value ||= node.id || "";
}

function graphParams() {
  const params = new URLSearchParams();
  const root = $("graphRoot").value.trim();
  const q = $("graphSearch").value.trim();
  const category = $("graphCategory").value.trim();
  const friendMin = $("graphFriendCountMin").value.trim();
  const friendMax = $("graphFriendCountMax").value.trim();
  if (root) params.set("root", root);
  if (q) params.set("q", q);
  if (category) params.set("category", category);
  if (friendMin) params.set("friend_count_min", friendMin);
  if (friendMax) params.set("friend_count_max", friendMax);
  params.set("prior_pool_min_links", $("graphPriorPoolMinLinks").value || "0");
  params.set("sort_by", $("graphSortBy").value || "depth");
  params.set("sort_dir", $("graphSortDir").value || "asc");
  params.set("depth", $("graphDepth").value || "2");
  params.set("limit", $("graphLimit").value || "500");
  return params;
}

function validateGraphFilters() {
  clearFieldErrors(["graphFriendCountMin", "graphFriendCountMax", "graphPriorPoolMinLinks", "graphDepth", "graphLimit"]);
  if (!validateRange("graphFriendCountMin", "graphFriendCountMax")) return false;
  const prior = numberValue("graphPriorPoolMinLinks", 0);
  if (prior < 0) {
    setFieldError("graphPriorPoolMinLinks", t("validation.nonNegative"));
    return false;
  }
  return true;
}

async function loadGraph() {
  if (!validateGraphFilters()) throw new Error(t("validation.fixFields"));
  try {
    const data = await api(`/api/graph?${graphParams().toString()}`);
    renderGraph(data);
    appendSystemLog("info", "graph", t("log.graphLoaded", { nodes: data.nodes.length, edges: data.edges.length }));
  } catch (error) {
    appendUiLog("error", t("graph.loadFailed"), error.message);
    toast(t("toast.graphLoadFailed"));
    throw error;
  }
}

async function loadDbStats() {
  const stats = await api("/api/db/stats");
  $("dbSteamUsers").textContent = stats.steam_users;
  $("dbRelationships").textContent = stats.steam_friend_relationships;
  $("dbCrawlRuns").textContent = stats.crawl_runs;
}

function secretLabel(configured, fromEnv) {
  if (fromEnv) return t("secret.env");
  return configured ? t("secret.configured") : t("secret.missing");
}

async function loadSettings() {
  const settings = await api("/api/settings");
  $("settingsNeo4jUri").value = settings.neo4j_uri || "";
  $("settingsNeo4jUser").value = settings.neo4j_user || "";
  $("steamSecretState").textContent = secretLabel(settings.steam_api_key_configured, settings.steam_api_key_from_env);
  $("neo4jSecretState").textContent = secretLabel(settings.neo4j_password_configured, settings.neo4j_password_from_env);
  $("settingsMessage").textContent = settings.message || "";
}

async function saveSettings() {
  clearFieldErrors(["settingsNeo4jUri", "settingsNeo4jUser"]);
  if (!$("settingsNeo4jUri").value.trim()) {
    setFieldError("settingsNeo4jUri", t("validation.required"));
    throw new Error(t("validation.fixFields"));
  }
  if (!$("settingsNeo4jUser").value.trim()) {
    setFieldError("settingsNeo4jUser", t("validation.required"));
    throw new Error(t("validation.fixFields"));
  }
  const payload = {
    neo4j_uri: $("settingsNeo4jUri").value.trim(),
    neo4j_user: $("settingsNeo4jUser").value.trim(),
  };
  await api("/api/settings", { method: "PATCH", body: JSON.stringify(payload) });
  const steamKey = $("steamApiKeyInput").value.trim();
  const neo4jPassword = $("neo4jPasswordInput").value;
  if (steamKey) {
    await api("/api/settings/secrets", { method: "POST", body: JSON.stringify({ name: "steam_api_key", value: steamKey }) });
  }
  if (neo4jPassword) {
    await api("/api/settings/secrets", { method: "POST", body: JSON.stringify({ name: "neo4j_password", value: neo4jPassword }) });
  }
  $("steamApiKeyInput").value = "";
  $("neo4jPasswordInput").value = "";
  await loadSettings();
  toast(t("toast.settingsSaved"));
}

async function testSettings() {
  setStatus("steamStatus", "testing");
  setStatus("neo4jStatus", "testing");
  const result = await api("/api/settings/test", { method: "POST", body: "{}" });
  setStatus("steamStatus", result.steam_ok ? "ok" : "failed");
  setStatus("neo4jStatus", result.neo4j_ok ? "ok" : "failed");
  toast(`${result.steam_message} · ${result.neo4j_message}`);
  appendSystemLog(result.steam_ok && result.neo4j_ok ? "info" : "warn", "settings", `${result.steam_message} · ${result.neo4j_message}`);
  await loadDbStats().catch(() => {});
}

function validateCrawlPayload() {
  clearFieldErrors(["rootUrl", "maxDepth", "maxNodes", "delayMs", "crawlFriendCountMin", "crawlFriendCountMax", "crawlPriorPoolMinLinks"]);
  let ok = true;
  if (!$("rootUrl").value.trim()) {
    setFieldError("rootUrl", t("validation.required"));
    ok = false;
  }
  if (!validateRange("crawlFriendCountMin", "crawlFriendCountMax")) ok = false;
  const checks = [
    ["maxDepth", 1, 4],
    ["maxNodes", 1, 10000],
    ["delayMs", 0, 10000],
    ["crawlPriorPoolMinLinks", 0, Number.MAX_SAFE_INTEGER],
  ];
  for (const [id, min, max] of checks) {
    const value = numberValue(id, 0);
    if (value < min || value > max) {
      setFieldError(id, t("validation.range", { min, max }));
      ok = false;
    }
  }
  return ok;
}

async function startCrawl() {
  if (!validateCrawlPayload()) throw new Error(t("validation.fixFields"));
  const payload = {
    root_url: $("rootUrl").value.trim(),
    max_depth: Number($("maxDepth").value || 2),
    max_nodes: Number($("maxNodes").value || 2000),
    delay_ms: Number($("delayMs").value || 300),
    prior_pool_min_links: Number($("crawlPriorPoolMinLinks").value || 0),
  };
  const friendMin = $("crawlFriendCountMin").value.trim();
  const friendMax = $("crawlFriendCountMax").value.trim();
  if (friendMin) payload.friend_count_min = Number(friendMin);
  if (friendMax) payload.friend_count_max = Number(friendMax);
  const run = await api("/api/crawls", { method: "POST", body: JSON.stringify(payload) });
  currentRunId = run.id;
  lastEventSeq = 0;
  $("crawlLogs").innerHTML = "";
  setProgress(1);
  $("graphRoot").value = run.root_steam_id;
  $("analysisRoot").value = run.root_steam_id;
  toast(t("toast.crawlStarted"));
  appendSystemLog("info", "crawl", t("toast.crawlStarted"));
  pollRun();
}

async function pollRun() {
  if (!currentRunId) return;
  clearTimeout(pollTimer);
  const run = await api(`/api/crawls/${currentRunId}`);
  setStatus("crawlStatus", run.status);
  $("nodeCount").textContent = run.nodes_discovered;
  $("edgeCount").textContent = run.edges_discovered;
  $("privateCount").textContent = run.private_count;
  $("filteredCount").textContent = run.filtered_count || 0;
  setProgress(run.progress_percent);
  if (run.last_event) $("lastEvent").textContent = run.last_event;
  await loadEvents().catch(() => {});
  if (["completed", "cancelled", "failed"].includes(run.status)) {
    toast(run.message || statusText(run.status));
    appendSystemLog(run.status === "failed" ? "error" : "info", "crawl", run.message || statusText(run.status));
    await loadGraph().catch(() => {});
    await loadDbStats().catch(() => {});
    return;
  }
  pollTimer = setTimeout(pollRun, 1200);
}

async function loadEvents() {
  if (!currentRunId) return;
  const events = await api(`/api/crawls/${currentRunId}/events?after=${lastEventSeq}`);
  for (const event of events) {
    appendUiLog(event.level, event.stage, event.message, event.time);
    lastEventSeq = Math.max(lastEventSeq, event.seq);
  }
}

async function loadSystemLogs(reset = false) {
  if (reset) {
    lastSystemLogSeq = 0;
    $("systemLogs").innerHTML = "";
  }
  const params = new URLSearchParams();
  params.set("after", String(lastSystemLogSeq));
  const level = $("systemLogLevel").value;
  if (level) params.set("level", level);
  const rows = await api(`/api/logs?${params.toString()}`);
  for (const row of rows) {
    appendSystemLog(row.level, row.source, row.message, row.time);
    lastSystemLogSeq = Math.max(lastSystemLogSeq, row.seq);
  }
}

function startSystemLogPolling() {
  clearInterval(systemLogTimer);
  systemLogTimer = setInterval(() => loadSystemLogs().catch(() => {}), 2500);
}

async function cancelCrawl() {
  if (!currentRunId) {
    toast(t("toast.noActiveCrawl"));
    return;
  }
  await api(`/api/crawls/${currentRunId}/cancel`, { method: "POST", body: "{}" });
  toast(t("toast.cancelRequested"));
}

async function saveProfile() {
  if (!selectedNode?.id) {
    toast(t("toast.selectNodeFirst"));
    return;
  }
  await api(`/api/users/${selectedNode.id}`, {
    method: "PATCH",
    body: JSON.stringify({
      category: $("profileCategory").value.trim(),
      tags: $("profileTags").value.split(",").map((item) => item.trim()).filter(Boolean),
      note: $("profileNote").value,
    }),
  });
  toast(t("toast.profileSaved"));
  await loadGraph();
}

async function findPath() {
  const from = $("pathFrom").value.trim();
  const to = $("pathTo").value.trim();
  clearFieldErrors(["pathFrom", "pathTo"]);
  if (!from || !to) {
    if (!from) setFieldError("pathFrom", t("validation.required"));
    if (!to) setFieldError("pathTo", t("validation.required"));
    throw new Error(t("toast.fromToRequired"));
  }
  const data = await api(`/api/path?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}&max_depth=4`);
  if (!data.nodes.length) {
    $("pathResult").dataset.state = "no-path";
    $("pathResult").textContent = t("path.noPath");
    return;
  }
  $("pathResult").dataset.state = "path";
  renderGraph(data);
  $("pathResult").textContent = data.nodes.map((node) => node.label || node.id).join(" -> ");
}

async function loadTopDegree() {
  const rows = await api("/api/stats/top-degree?limit=12");
  $("topDegreeList").innerHTML = rows
    .map((node) => `<li><strong>${escapeHtml(node.label)}</strong> · ${node.degree}</li>`)
    .join("");
}

async function loadFriendCircles() {
  clearFieldErrors(["analysisRoot", "analysisMaxDepth", "analysisMinMutual", "analysisLimit"]);
  const root = $("analysisRoot").value.trim() || $("graphRoot").value.trim();
  if (!root) {
    setFieldError("analysisRoot", t("validation.required"));
    throw new Error(t("validation.rootSteamIdRequired"));
  }
  $("analysisRoot").value = root;
  const params = new URLSearchParams({
    root,
    max_depth: $("analysisMaxDepth").value || "3",
    min_mutual: $("analysisMinMutual").value || "2",
    limit: $("analysisLimit").value || "30",
  });
  const data = await api(`/api/analysis/friend-circles?${params.toString()}`);
  $("friendCircleList").innerHTML = data.candidates
    .map(
      (item) =>
        `<li><button class="rank-button" data-steam-id="${escapeHtml(item.steam_id)}"><strong>${escapeHtml(item.label)}</strong><span>${t("analysis.row", {
          mutual: item.mutual_count,
          score: item.score,
        })}</span></button></li>`,
    )
    .join("");
  document.querySelectorAll(".rank-button").forEach((button) => {
    button.addEventListener("click", () => focusAnalysisCandidate(button.dataset.steamId, data.candidates));
  });
  toast(t("toast.analysisLoaded"));
}

function focusAnalysisCandidate(steamId, candidates) {
  const candidate = candidates.find((item) => item.steam_id === steamId);
  cy.elements().removeClass("analysis-focus analysis-evidence");
  const node = cy.getElementById(steamId);
  if (node.length) {
    node.addClass("analysis-focus");
    cy.center(node);
  }
  for (const evidence of candidate?.evidence || []) {
    const evidenceNode = cy.getElementById(evidence.id);
    if (evidenceNode.length) evidenceNode.addClass("analysis-evidence");
  }
  appendSystemLog("info", "analysis", t("analysis.focused", { label: candidate?.label || steamId }));
}

async function exportFile(format) {
  try {
    const response = await fetch("/api/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ format }),
    });
    if (!response.ok) throw new Error(await response.text());
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `steam_graph.${format}`;
    link.click();
    URL.revokeObjectURL(url);
  } catch (err) {
    toast(`Export failed: ${err.message}`);
    return;
  }
  toast(t(format === "csv" ? "toast.exportCsv" : "toast.exportJson"));
}

async function copySystemLogs() {
  const text = Array.from($("systemLogs").querySelectorAll(".log-item"))
    .map((row) => row.textContent.trim())
    .join("\n");
  await navigator.clipboard.writeText(text);
  toast(t("toast.logsCopied"));
}

function wireEvents() {
  document.querySelectorAll(".lang-button").forEach((button) => {
    button.addEventListener("click", () => setLanguage(button.dataset.lang));
  });
  $("testSettings").addEventListener("click", (event) => withButtonState(event.currentTarget, testSettings).catch(() => {}));
  $("loadSettings").addEventListener("click", (event) => withButtonState(event.currentTarget, loadSettings).catch(() => {}));
  $("saveSettings").addEventListener("click", (event) => withButtonState(event.currentTarget, saveSettings).catch(() => {}));
  $("refreshDbStats").addEventListener("click", (event) => withButtonState(event.currentTarget, loadDbStats).catch(() => {}));
  $("startCrawl").addEventListener("click", (event) => withButtonState(event.currentTarget, startCrawl).catch(() => {}));
  $("cancelCrawl").addEventListener("click", (event) => withButtonState(event.currentTarget, cancelCrawl).catch(() => {}));
  $("refreshGraph").addEventListener("click", (event) => withButtonState(event.currentTarget, loadGraph).catch(() => {}));
  $("fitGraph").addEventListener("click", (event) => withButtonState(event.currentTarget, async () => cy.fit(undefined, 40)).catch(() => {}));
  $("layoutGraph").addEventListener("click", (event) => withButtonState(event.currentTarget, async () => runLayout()).catch(() => {}));
  $("saveProfile").addEventListener("click", (event) => withButtonState(event.currentTarget, saveProfile).catch(() => {}));
  $("findPath").addEventListener("click", (event) => withButtonState(event.currentTarget, findPath).catch(() => {}));
  $("loadTopDegree").addEventListener("click", (event) => withButtonState(event.currentTarget, loadTopDegree).catch(() => {}));
  $("loadFriendCircles").addEventListener("click", (event) => withButtonState(event.currentTarget, loadFriendCircles).catch(() => {}));
  $("refreshSystemLogs").addEventListener("click", (event) => withButtonState(event.currentTarget, () => loadSystemLogs(true)).catch(() => {}));
  $("copySystemLogs").addEventListener("click", (event) => withButtonState(event.currentTarget, copySystemLogs).catch(() => {}));
  $("clearSystemLogs").addEventListener("click", () => {
    $("systemLogs").innerHTML = "";
    toast(t("toast.logsCleared"));
  });
  $("systemLogLevel").addEventListener("change", () => loadSystemLogs(true).catch(() => {}));
  $("graphSizeBy").addEventListener("change", () => renderGraph(currentGraph));
  $("graphLayoutBias").addEventListener("change", runLayout);
  $("exportJson").addEventListener("click", (event) => withButtonState(event.currentTarget, async () => exportFile("json")).catch(() => {}));
  $("exportCsv").addEventListener("click", (event) => withButtonState(event.currentTarget, async () => exportFile("csv")).catch(() => {}));
  $("copyBloom").addEventListener("click", (event) =>
    withButtonState(event.currentTarget, async () => {
      await navigator.clipboard.writeText($("bloomQuery").value);
      toast(t("toast.copied"));
    }).catch(() => {}),
  );
}

window.addEventListener("error", (event) => {
  appendSystemLog("error", "frontend", event.message);
});

window.addEventListener("unhandledrejection", (event) => {
  appendSystemLog("error", "frontend", event.reason?.message || String(event.reason));
});

document.addEventListener("DOMContentLoaded", async () => {
  await loadI18n();
  applyTranslations();
  if (window.lucide) window.lucide.createIcons();
  initGraph();
  wireEvents();
  $("pathResult").dataset.state = "empty";
  loadSettings().catch((error) => appendSystemLog("error", "settings", error.message));
  loadGraph().catch(() => {});
  loadDbStats().catch((error) => appendSystemLog("error", "db", error.message));
  loadTopDegree().catch((error) => appendSystemLog("error", "stats", error.message));
  loadSystemLogs(true).catch(() => {});
  startSystemLogPolling();
});
