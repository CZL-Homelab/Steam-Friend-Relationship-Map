const $ = (id) => document.getElementById(id);

const FALLBACK_ZH = {
  "app.title": "Steam 好友关系图谱",
  "app.subtitle": "Neo4j 本地图数据库",
  "graph.summary": "{nodes} 个节点 · {edges} 条关系",
  "graph.summaryLimited": "{nodes} 个节点 · {edges} 条关系 · 已限制",
  "graph.loadFailed": "图谱加载失败",
  "log.empty": "暂无日志",
  "path.empty": "未选择路径",
  "path.noPath": "没有路径",
  "profile.empty": "选择一个节点",
  "profile.steamProfile": "Steam 主页",
  "status.cancelled": "已取消",
  "status.completed": "已完成",
  "status.failed": "失败",
  "status.idle": "空闲",
  "status.ok": "正常",
  "status.pending": "等待中",
  "status.private": "私密",
  "status.public": "公开",
  "status.running": "运行中",
  "status.testing": "测试中",
  "status.unknown": "未知",
  "toast.cancelRequested": "已请求取消",
  "toast.copied": "已复制",
  "toast.crawlStarted": "抓取已开始",
  "toast.fromToRequired": "请输入起点和终点",
  "toast.profileSaved": "资料已保存",
  "toast.rootRequired": "请输入 Root URL",
  "toast.settingsSaved": "配置已保存",
  "toast.graphLoadFailed": "图谱加载失败，详情见日志",
};

let cy;
let currentRunId = null;
let pollTimer = null;
let selectedNode = null;
let currentGraph = { nodes: [], edges: [], limited: false };
let i18n = { "zh-CN": FALLBACK_ZH, en: {} };
let currentLang = localStorage.getItem("sfm_lang") || "zh-CN";
let lastEventSeq = 0;

// 前端不引入构建系统，翻译文件直接作为静态 JSON 加载。
async function loadI18n() {
  try {
    const response = await fetch("/static/i18n.json");
    if (response.ok) {
      i18n = await response.json();
    }
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

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.detail || response.statusText);
  }
  return response.json();
}

function appendUiLog(level, stage, message, time = new Date().toISOString()) {
  const list = $("crawlLogs");
  const row = document.createElement("div");
  row.className = `log-item log-${level}`;
  row.innerHTML = `<span class="log-meta">${escapeHtml(time)} · ${escapeHtml(stage)}</span><span>${escapeHtml(message)}</span>`;
  list.appendChild(row);
  while (list.children.length > 300) list.removeChild(list.firstElementChild);
  list.scrollTop = list.scrollHeight;
  $("lastEvent").textContent = message;
}

function setProgress(percent) {
  $("crawlProgressBar").style.width = `${Math.max(0, Math.min(100, Number(percent) || 0))}%`;
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
          width: "mapData(degree, 0, 100, 34, 86)",
          height: "mapData(degree, 0, 100, 34, 86)",
        },
      },
      {
        selector: "node[status = 'private']",
        style: { "border-color": "#be123c", "border-width": 3 },
      },
      {
        selector: "edge",
        style: {
          width: 1.4,
          "line-color": "#9aa8b2",
          opacity: 0.62,
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

function renderGraph(data) {
  currentGraph = data;
  const elements = [
    ...data.nodes.map((node) => ({
      data: {
        id: node.id,
        label: node.label,
        avatar: node.avatar,
        degree: node.degree || 1,
        status: node.friend_list_status,
        node,
      },
    })),
    ...data.edges.map((edge) => ({ data: { id: edge.id, source: edge.source, target: edge.target } })),
  ];
  cy.elements().remove();
  cy.add(elements);
  runLayout();
  updateGraphSummary();
  $("graphEmpty").classList.toggle("hidden", data.nodes.length > 0);
}

function updateGraphSummary() {
  const key = currentGraph.limited ? "graph.summaryLimited" : "graph.summary";
  $("graphSummary").textContent = t(key, {
    nodes: currentGraph.nodes.length,
    edges: currentGraph.edges.length,
  });
}

// Cytoscape 的 cose 布局适合中小规模社交网络，刷新和路径结果都复用这一套布局。
function runLayout() {
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
  $("profileStatus").dataset.status = node.friend_list_status || "unknown";
  $("profileStatus").textContent = statusText(node.friend_list_status);
  $("profileCategory").value = node.category || "";
  $("profileTags").value = (node.tags || []).join(", ");
  $("profileNote").value = node.note || "";
  $("pathFrom").value ||= node.id || "";
}

async function loadGraph() {
  const params = new URLSearchParams();
  const root = $("graphRoot").value.trim();
  const q = $("graphSearch").value.trim();
  const category = $("graphCategory").value.trim();
  if (root) params.set("root", root);
  if (q) params.set("q", q);
  if (category) params.set("category", category);
  params.set("depth", $("graphDepth").value || "2");
  params.set("limit", $("graphLimit").value || "500");
  try {
    const data = await api(`/api/graph?${params.toString()}`);
    renderGraph(data);
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
  await loadDbStats().catch(() => {});
}

async function startCrawl() {
  const payload = {
    root_url: $("rootUrl").value.trim(),
    max_depth: Number($("maxDepth").value || 2),
    max_nodes: Number($("maxNodes").value || 2000),
    delay_ms: Number($("delayMs").value || 300),
  };
  if (!payload.root_url) {
    toast(t("toast.rootRequired"));
    return;
  }
  const run = await api("/api/crawls", { method: "POST", body: JSON.stringify(payload) });
  currentRunId = run.id;
  lastEventSeq = 0;
  $("crawlLogs").innerHTML = "";
  setProgress(1);
  $("graphRoot").value = run.root_steam_id;
  toast(t("toast.crawlStarted"));
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
  setProgress(run.progress_percent);
  if (run.last_event) $("lastEvent").textContent = run.last_event;
  await loadEvents().catch(() => {});
  if (["completed", "cancelled", "failed"].includes(run.status)) {
    toast(run.message || statusText(run.status));
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

async function cancelCrawl() {
  if (!currentRunId) return;
  await api(`/api/crawls/${currentRunId}/cancel`, { method: "POST", body: "{}" });
  toast(t("toast.cancelRequested"));
}

async function saveProfile() {
  if (!selectedNode?.id) return;
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
  if (!from || !to) {
    toast(t("toast.fromToRequired"));
    return;
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

function exportFile(format) {
  window.location.href = `/api/export?format=${format}`;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function wireEvents() {
  document.querySelectorAll(".lang-button").forEach((button) => {
    button.addEventListener("click", () => setLanguage(button.dataset.lang));
  });
  $("testSettings").addEventListener("click", () => testSettings().catch((error) => toast(error.message)));
  $("loadSettings").addEventListener("click", () => loadSettings().catch((error) => toast(error.message)));
  $("saveSettings").addEventListener("click", () => saveSettings().catch((error) => toast(error.message)));
  $("refreshDbStats").addEventListener("click", () => loadDbStats().catch((error) => toast(error.message)));
  $("startCrawl").addEventListener("click", () => startCrawl().catch((error) => toast(error.message)));
  $("cancelCrawl").addEventListener("click", () => cancelCrawl().catch((error) => toast(error.message)));
  $("refreshGraph").addEventListener("click", () => loadGraph().catch((error) => toast(error.message)));
  $("fitGraph").addEventListener("click", () => cy.fit(undefined, 40));
  $("layoutGraph").addEventListener("click", runLayout);
  $("saveProfile").addEventListener("click", () => saveProfile().catch((error) => toast(error.message)));
  $("findPath").addEventListener("click", () => findPath().catch((error) => toast(error.message)));
  $("loadTopDegree").addEventListener("click", () => loadTopDegree().catch((error) => toast(error.message)));
  $("exportJson").addEventListener("click", () => exportFile("json"));
  $("exportCsv").addEventListener("click", () => exportFile("csv"));
  $("copyBloom").addEventListener("click", async () => {
    await navigator.clipboard.writeText($("bloomQuery").value);
    toast(t("toast.copied"));
  });
}

document.addEventListener("DOMContentLoaded", async () => {
  await loadI18n();
  applyTranslations();
  if (window.lucide) window.lucide.createIcons();
  initGraph();
  wireEvents();
  $("pathResult").dataset.state = "empty";
  loadSettings().catch((error) => appendUiLog("error", "settings", error.message));
  loadGraph().catch(() => {});
  loadDbStats().catch((error) => appendUiLog("error", "db", error.message));
  loadTopDegree().catch((error) => appendUiLog("error", "stats", error.message));
});
