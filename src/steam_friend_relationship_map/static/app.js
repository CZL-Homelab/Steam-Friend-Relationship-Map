const $ = (id) => document.getElementById(id);

const FALLBACK_ZH = {
  "app.title": "Steam 好友关系图谱",
  "app.subtitle": "Neo4j 本地图数据库",
  "graph.summary": "{nodes} 个节点 · {edges} 条关系",
  "graph.summaryLimited": "{nodes} 个节点 · {edges} 条关系 · 已限制",
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
};

let cy;
let currentRunId = null;
let pollTimer = null;
let selectedNode = null;
let currentGraph = { nodes: [], edges: [], limited: false };
let i18n = { "zh-CN": FALLBACK_ZH, en: {} };
let currentLang = localStorage.getItem("sfm_lang") || "zh-CN";

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
  $("profileAvatar").src = node.avatar || "";
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
  const data = await api(`/api/graph?${params.toString()}`);
  renderGraph(data);
}

async function testSettings() {
  setStatus("steamStatus", "testing");
  setStatus("neo4jStatus", "testing");
  const result = await api("/api/settings/test", { method: "POST", body: "{}" });
  setStatus("steamStatus", result.steam_ok ? "ok" : "failed");
  setStatus("neo4jStatus", result.neo4j_ok ? "ok" : "failed");
  toast(`${result.steam_message} · ${result.neo4j_message}`);
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
  if (["completed", "cancelled", "failed"].includes(run.status)) {
    toast(run.message || statusText(run.status));
    await loadGraph().catch(() => {});
    return;
  }
  pollTimer = setTimeout(pollRun, 1200);
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
  loadGraph().catch(() => {});
  loadTopDegree().catch(() => {});
});
