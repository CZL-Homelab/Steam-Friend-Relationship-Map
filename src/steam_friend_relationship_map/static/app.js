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
  "connection.notTested": "尚未测试",
  "status.idle": "空闲",
  "status.unknown": "未知",
  "toast.rootRequired": "请输入 Root URL",
  "toast.graphLoadFailed": "图谱加载失败，详情见日志",
};

let cy;
let currentRunId = null;
let pollTimer = null;
let systemLogTimer = null;
let timerInterval = null;
let crawlStartTime = null;
let dbStatsTimer = null;
let selectedNode = null;
let currentGraph = { nodes: [], edges: [], limited: false };
let currentNetworkAnalysis = null;
let i18n = { "zh-CN": FALLBACK_ZH, en: {} };
let currentLang = localStorage.getItem("sfm_lang") || "zh-CN";
let lastEventSeq = 0;
let lastSystemLogSeq = 0;
const requestCoordinator = new window.LatestRequestCoordinator();
const graphLifecycle = new window.GraphLifecycleCoordinator();
const PROJECT_SCOPED_REQUEST_KEYS = [
  "graph",
  "db-stats",
  "projects",
  "settings",
  "network-analysis",
  "friend-circles",
];

const COMMUNITY_COLORS = [
  "#16a34a", "#dc2626", "#d97706", "#7c3aed", "#0891b2", "#c026d3",
  "#65a30d", "#ea580c", "#4f46e5", "#0f766e", "#be123c", "#0369a1",
];

async function loadI18n() {
  try {
    const response = await fetch(`/static/i18n.json?t=${Date.now()}`);
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
  if (currentNetworkAnalysis) renderNetworkAnalysisResults(currentNetworkAnalysis);
  if (selectedNode) fillProfile(selectedNode);
}

// ── Theme ─────────────────────────────────────────────────────────

function initTheme() {
  const saved = localStorage.getItem("sfm_theme") || "auto";
  applyTheme(saved);
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    if ((localStorage.getItem("sfm_theme") || "auto") === "auto") {
      applyTheme("auto");
    }
  });
}

function getCssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function updateCytoscapeStyle() {
  if (!cy) return;
  const ink = getCssVar("--ink");
  const panel = getCssVar("--panel");
  const teal = getCssVar("--teal");
  const blue = getCssVar("--blue");
  const rose = getCssVar("--rose");
  const amber = getCssVar("--amber");
  const muted = getCssVar("--muted");
  const isDark = document.documentElement.getAttribute("data-theme") === "dark";

  const style = cy.style()
    .selector("node")
    .style({
      "background-color": teal,
      "border-color": panel,
      color: ink,
      "text-background-color": panel,
      "shadow-blur": isDark ? 8 : 0,
      "shadow-color": teal,
      "shadow-opacity": isDark ? 0.65 : 0,
      "shadow-offset-x": 0,
      "shadow-offset-y": 0,
    });
  COMMUNITY_COLORS.forEach((color, index) => {
    style.selector(`node[community = ${index + 1}][hasCommunity = 1]`).style({
      "border-color": color,
      "border-width": 4,
    });
  });
  style
    .selector("node[status = 'private']")
    .style({
      "border-color": rose,
    })
    .selector("node.analysis-focus")
    .style({
      "border-color": blue,
    })
    .selector("node.analysis-evidence")
    .style({
      "border-color": amber,
    })
    .selector("edge")
    .style({
      "line-color": muted,
    })
    .selector(":selected")
    .style({
      "border-color": blue,
      "line-color": blue,
    })
    .update();
}

function updateThemeToggleIcon(mode) {
  const toggle = $("themeToggle");
  if (!toggle) return;
  let iconName = "sun-moon";
  if (mode === "light") iconName = "sun";
  else if (mode === "dark") iconName = "moon";
  toggle.innerHTML = `<i data-lucide="${iconName}"></i>`;
  if (window.lucide) window.lucide.createIcons();
}

function applyTheme(mode) {
  if (mode === "dark") {
    document.documentElement.setAttribute("data-theme", "dark");
  } else if (mode === "light") {
    document.documentElement.removeAttribute("data-theme");
  } else {
    const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    document.documentElement.toggleAttribute("data-theme", prefersDark);
  }
  localStorage.setItem("sfm_theme", mode);
  updateCytoscapeStyle();
  updateThemeToggleIcon(mode);
}

function cycleTheme() {
  const modes = ["auto", "light", "dark"];
  const current = localStorage.getItem("sfm_theme") || "auto";
  const next = modes[(modes.indexOf(current) + 1) % modes.length];
  applyTheme(next);
  toast(t(`theme.${next}`));
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
  if ($("profileUrlText")) $("profileUrlText").textContent = t("profile.steamProfile");
  if ($("pathResult").dataset.state === "empty") $("pathResult").textContent = t("path.empty");
  if ($("pathResult").dataset.state === "no-path") $("pathResult").textContent = t("path.noPath");
  if (!$("crawlLogs").children.length) $("lastEvent").textContent = t("log.empty");
  document.querySelectorAll(".connection-detail").forEach((node) => {
    const raw = node.dataset.rawMessage;
    if (raw) {
      const isOk = node.classList.contains("ok");
      const isFailed = node.classList.contains("failed");
      const okState = isOk ? true : (isFailed ? false : null);
      setConnectionDetail(node.id, raw, okState);
    } else {
      node.textContent = t("connection.notTested");
    }
  });
  const activeProj = $("activeProjectName");
  if (activeProj) {
    const projName = activeProj.textContent.trim();
    if (projName === "default" || projName === "Default Project" || projName === "默认项目") {
      activeProj.textContent = t("project.defaultName");
    }
  }
  loadProjects().catch(() => {});
}

function statusText(status) {
  return t(`status.${status || "unknown"}`);
}

function setStatus(id, status) {
  const node = $(id);
  node.dataset.status = status;
  node.textContent = statusText(status);
}

function setConnectionDetail(id, message, ok = null) {
  const node = $(id);
  if (!node) return;
  
  if (message) {
    node.dataset.rawMessage = message;
  }
  
  let translatedMessage = message;
  if (currentLang === "en") {
    if (message === "Steam API Key 可用") {
      translatedMessage = "Steam API Key is valid";
    } else if (message === "Kùzu 连接正常") {
      translatedMessage = "Kùzu connection is normal";
    } else if (message === "Neo4j 连接正常") {
      translatedMessage = "Neo4j connection is normal";
    } else if (message && message.startsWith("连接测试失败：")) {
      translatedMessage = "Connection test failed: " + message.substring(7);
    }
  } else {
    if (message === "Steam API Key is valid") {
      translatedMessage = "Steam API Key 可用";
    } else if (message === "Kùzu connection is normal") {
      translatedMessage = "Kùzu 连接正常";
    } else if (message === "Neo4j connection is normal") {
      translatedMessage = "Neo4j 连接正常";
    }
  }
  
  node.textContent = translatedMessage || t("connection.notTested");
  node.classList.toggle("ok", ok === true);
  node.classList.toggle("failed", ok === false);
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
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

async function api(path, options = {}) {
  try {
    const { headers = {}, ...fetchOptions } = options;
    const response = await fetch(path, {
      ...fetchOptions,
      headers: { "Content-Type": "application/json", ...headers },
    });
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      throw new Error(detail.detail || response.statusText);
    }
    return response.json();
  } catch (error) {
    if (error.name !== "AbortError") {
      appendSystemLog("error", "api", `${path.split("?")[0]}: ${error.message}`);
    }
    throw error;
  }
}

async function latestApi(key, path, options = {}) {
  const request = requestCoordinator.begin(key);
  try {
    const data = await api(path, { ...options, signal: request.signal });
    return request.isCurrent() ? data : null;
  } catch (error) {
    if (error.name === "AbortError" || !request.isCurrent()) return null;
    throw error;
  } finally {
    request.finish();
  }
}

function cancelProjectScopedRequests() {
  requestCoordinator.cancelMany(PROJECT_SCOPED_REQUEST_KEYS);
}

function formatLogTime(isoString) {
  try {
    const d = new Date(isoString);
    if (isNaN(d.getTime())) return isoString;
    const pad = (n) => String(n).padStart(2, "0");
    const yyyy = d.getFullYear();
    const mm = pad(d.getMonth() + 1);
    const dd = pad(d.getDate());
    const hh = pad(d.getHours());
    const min = pad(d.getMinutes());
    const ss = pad(d.getSeconds());
    return `${yyyy}-${mm}-${dd} ${hh}:${min}:${ss}`;
  } catch {
    return isoString;
  }
}

function appendLog(listId, level, source, message, time = new Date().toISOString()) {
  const list = $(listId);
  if (!list) return;
  const row = document.createElement("div");
  row.className = `log-item log-${level}`;
  row.dataset.level = level;
  
  const formattedTime = formatLogTime(time);
  const levelTag = level ? ` <span class="log-tag-${level}">[${level.toUpperCase()}]</span>` : "";
  row.innerHTML = `<span class="log-meta">${escapeHtml(formattedTime)}${levelTag} · ${escapeHtml(source)}</span><span>${escapeHtml(message)}</span>`;
  
  if (listId === "crawlLogs" && $("crawlLogLevel")) {
    const selectedLevel = $("crawlLogLevel").value;
    if (selectedLevel && level !== selectedLevel) {
      row.style.display = "none";
    }
  }
  
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
    Haptic.trigger('success');
    setTimeout(() => node.classList.remove("button-success"), 900);
    return result;
  } catch (error) {
    node.classList.add("button-error");
    Haptic.trigger('error');
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
    pixelRatio: 1.0,           // 避免在高分屏（如 Mac Retina）下因超高分辨率渲染导致的严重卡顿
    motionBlur: false,          // 禁用动态模糊以节省渲染开销
    textureOnViewport: true,    // 缩放/平移时将视口作为纹理渲染，极大提升操作流畅度
    boxSelectionEnabled: false, // 禁用多选框以减少鼠标事件计算开销
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
        selector: "node[hasCommunity = 1]",
        style: { "border-width": 4 },
      },
      ...COMMUNITY_COLORS.map((color, index) => ({
        selector: `node[community = ${index + 1}][hasCommunity = 1]`,
        style: { "border-color": color, "border-width": 4 },
      })),
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

  updateCytoscapeStyle();

  cy.on("tap", "node", (event) => {
    selectedNode = event.target.data().node;
    fillProfile(selectedNode);
  });
}

function metricValue(node, metric) {
  if (metric === "root_friend_circle") return node.root_friend_circle_score ?? 0;
  if (metric === "friend_count") return node.friend_count ?? 0;
  if (metric === "prior_pool_links") return node.prior_pool_link_count ?? 0;
  if (metric === "closeness") return node.root_closeness_score ?? 0;
  return node.degree ?? 0;
}

function isRootFriendCircleRoot(node) {
  return (node.root_friend_circle_score ?? 0) >= 1000000;
}

function buildRootFriendCircleScale(nodes) {
  const scores = nodes
    .filter((node) => !isRootFriendCircleRoot(node))
    .map((node) => node.root_friend_circle_score ?? 0)
    .filter((score) => score > 0);
  const uniqueScores = [...new Set(scores)].sort((a, b) => a - b);
  const rankByScore = new Map(uniqueScores.map((score, index) => [score, index + 1]));
  const maxRank = Math.max(1, uniqueScores.length);

  return {
    rank(node) {
      if (isRootFriendCircleRoot(node)) return maxRank + 2;
      return rankByScore.get(node.root_friend_circle_score ?? 0) || 0;
    },
    visualSize(node) {
      if (isRootFriendCircleRoot(node)) return 100;
      const rank = rankByScore.get(node.root_friend_circle_score ?? 0) || 0;
      if (!rank) return 8;
      return 28 + (rank / maxRank) * 60;
    },
  };
}

function renderGraph(data) {
  const renderId = graphLifecycle.beginRender();

  currentGraph = data;
  const sizeBy = $("graphSizeBy").value || "root_friend_circle";
  const maxMetric = Math.max(1, ...data.nodes.map((node) => metricValue(node, sizeBy)));
  const rootCircleScale = buildRootFriendCircleScale(data.nodes);
  const networkMetrics = new Map((currentNetworkAnalysis?.metrics || []).map((metric) => [metric.id, metric]));
  const useCommunityColors = $("communityColors")?.checked !== false;
  const fallbackBorder = getCssVar("--panel") || "#ffffff";
  const elements = [
    ...data.nodes.map((node) => {
      const networkMetric = networkMetrics.get(node.id);
      const hasCommunity = Boolean(networkMetric && useCommunityColors);
      const enrichedNode = networkMetric
        ? { ...node, pagerank: networkMetric.pagerank, network_community: networkMetric.community, community_size: networkMetric.community_size }
        : node;
      return {
        data: {
          id: node.id,
          label: node.label,
          avatar: node.avatar || "none",
          degree: node.degree || 1,
          closeness: node.root_closeness_score || 0,
          rootFriendCircle: node.root_friend_circle_score || 0,
          rootFriendCircleRank: rootCircleScale.rank(node),
          visualSize: sizeBy === "root_friend_circle"
            ? rootCircleScale.visualSize(node)
            : Math.max(5, Math.min(100, (metricValue(node, sizeBy) / maxMetric) * 100)),
          status: node.friend_list_status,
          pagerank: networkMetric?.pagerank || 0,
          community: networkMetric?.community || 0,
          hasCommunity: hasCommunity ? 1 : 0,
          communityColor: hasCommunity
            ? COMMUNITY_COLORS[(networkMetric.community - 1) % COMMUNITY_COLORS.length]
            : fallbackBorder,
          node: enrichedNode,
        },
      };
    }),
    ...data.edges.map((edge) => ({ data: { id: edge.id, source: edge.source, target: edge.target, strength: Math.max(1, edge.strength || 1) } })),
  ];

  cy.elements().remove();

  const loading = $("graphLoading");
  if (loading) loading.classList.remove("hidden");

  const chunkSize = 150;
  let index = 0;

  function addNextChunk() {
    if (!graphLifecycle.isCurrent(renderId)) return;

    if (index >= elements.length) {
      runLayout();
      updateGraphSummary();
      $("graphEmpty").classList.toggle("hidden", data.nodes.length > 0);
      if (!data.nodes.length) {
        $("graphEmpty").querySelector("p").textContent = t("graph.emptyFiltered");
      }
      if (loading) loading.classList.add("hidden");
      return;
    }

    const chunk = elements.slice(index, index + chunkSize);
    cy.add(chunk);
    index += chunkSize;

    graphLifecycle.scheduleChunk(renderId, addNextChunk, 10);
  }

  addNextChunk();
}

function updateGraphSummary() {
  const key = currentGraph.limited ? "graph.summaryLimited" : "graph.summary";
  $("graphSummary").textContent = t(key, {
    nodes: currentGraph.nodes.length,
    edges: currentGraph.edges.length,
  });
}

function runLayout() {
  const bias = $("graphLayoutBias")?.value || "root_friend_circle";
  const nodeCount = cy.nodes().length;
  // 大图模式下（如节点数 >= 300）禁用过渡动画，直接生成最终布局，能极大防止浏览器主线程假死
  const shouldAnimate = nodeCount < 300;

  if (bias === "root_friend_circle" || bias === "closeness") {
    graphLifecycle.startLayout(cy.layout({
      name: "concentric",
      animate: shouldAnimate ? "end" : false,
      animationDuration: 320,
      padding: 48,
      concentric: (node) => bias === "root_friend_circle"
        ? (node.data("rootFriendCircleRank") || node.data("closeness") || node.data("degree") || 1)
        : (node.data("closeness") || node.data("degree") || 1),
      levelWidth: () => bias === "root_friend_circle" ? 1 : 12,
    }));
    return;
  }
  graphLifecycle.startLayout(cy.layout({
    name: "cose",
    animate: shouldAnimate ? "end" : false,
    animationDuration: 320,
    padding: 48,
    nodeRepulsion: 9000,
    idealEdgeLength: 90,
    numIter: nodeCount > 500 ? 500 : 1000, // 大图下减少 cose 迭代次数以缩短运算耗时
  }));
}

function fillProfile(node) {
  selectedNode = node;
  $("profileAvatar").hidden = !node.avatar;
  if (node.avatar) $("profileAvatar").src = node.avatar;
  $("profileName").textContent = node.label || statusText("unknown");
  if (node.id) {
    $("profileHeaderLink").href = node.profile_url || "#";
    $("profileHeaderLink").setAttribute("target", "_blank");
    $("profileUrlLabel").style.display = "flex";
  } else {
    $("profileHeaderLink").href = "#";
    $("profileHeaderLink").removeAttribute("target");
    $("profileUrlLabel").style.display = "none";
  }
  $("profileSteamId").textContent = node.id || "-";
  $("profileDegree").textContent = node.degree ?? 0;
  $("profileFriendCount").textContent = node.friend_count ?? "-";
  $("profilePriorLinks").textContent = node.prior_pool_link_count ?? 0;
  $("profileCloseness").textContent = node.root_closeness_score ?? 0;
  $("profileRootRoutes").textContent = node.root_route_count ?? 0;
  $("profileRootRouteHops").textContent = node.root_route_total_hops ?? 0;
  $("profileRootFriendCircle").textContent = node.root_friend_circle_score ?? 0;
  const networkMetric = currentNetworkAnalysis?.metrics.find((metric) => metric.id === node.id);
  $("profilePageRank").textContent = networkMetric ? formatPageRank(networkMetric.pagerank) : "-";
  $("profileCommunity").textContent = networkMetric
    ? t("analysis.communityValue", { community: networkMetric.community, size: networkMetric.community_size })
    : "-";
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
    const data = await latestApi("graph", `/api/graph?${graphParams().toString()}`);
    if (!data) return;
    renderGraph(data);
    appendSystemLog("info", "graph", t("log.graphLoaded", { nodes: data.nodes.length, edges: data.edges.length }));
    if (data.depth_incomplete && data.root_found) {
      appendSystemLog("warn", "graph", t("graph.depthIncomplete", {
        reached: data.traversal_depth_reached ?? 0,
        requested: data.requested_depth ?? ($("graphDepth").value || 0),
      }));
    }
  } catch (error) {
    const message = error.message.includes("buffer pool is full")
      ? t("graph.memoryHint")
      : error.message;
    appendUiLog("error", t("graph.loadFailed"), message);
    toast(message === error.message ? t("toast.graphLoadFailed") : message);
    throw error;
  }
}

async function loadDbStats() {
  const stats = await latestApi("db-stats", "/api/db/stats");
  if (!stats) return;
  $("dbSteamUsers").textContent = stats.steam_users;
  $("dbRelationships").textContent = stats.steam_friend_relationships;
  $("dbCrawlRuns").textContent = stats.crawl_runs;
  // 如果当前项目有历史抓取，自动填入 Root 并加载图谱
  if (stats.latest_crawl && !$("graphRoot").value.trim()) {
    $("graphRoot").value = stats.latest_crawl.root_steam_id || "";
    $("analysisRoot").value = stats.latest_crawl.root_steam_id || "";
    loadGraph().catch(() => {});
  }
}

function startDbStatsPolling() {
  stopDbStatsPolling();
  const ms = parseInt($("dbStatsInterval").value) || 0;
  if (ms < 500) return;
  dbStatsTimer = setInterval(() => {
    loadDbStats().catch(() => {});
    loadProjects().catch(() => {});
  }, ms);
}

function stopDbStatsPolling() {
  clearInterval(dbStatsTimer);
  dbStatsTimer = null;
}

function secretLabel(configured, fromEnv) {
  if (fromEnv) return t("secret.env");
  return configured ? t("secret.configured") : t("secret.missing");
}

function isValidProxyUrl(value) {
  try {
    const parsed = new URL(value);
    return ["http:", "https:", "socks5:", "socks5h:"].includes(parsed.protocol) && Boolean(parsed.hostname);
  } catch (_) {
    return false;
  }
}

function toggleEngineSettings(engine) {
  if (engine === "kuzu") {
    $("kuzuSettingsGroup").style.display = "block";
    $("neo4jSettingsGroup").style.display = "none";
    if ($("insTabCypherBtn")) {
      $("insTabCypherBtn").style.display = "none";
    }
    const activeTabBtn = document.querySelector(".ins-tab-button.active");
    if (activeTabBtn && activeTabBtn.dataset.target === "insTabCypher") {
      const pathTabBtn = document.querySelector('.ins-tab-button[data-target="insTabPath"]');
      if (pathTabBtn) {
        pathTabBtn.click();
      }
    }
  } else {
    $("kuzuSettingsGroup").style.display = "none";
    $("neo4jSettingsGroup").style.display = "block";
    if ($("insTabCypherBtn")) {
      $("insTabCypherBtn").style.display = "inline-flex";
    }
  }
  if ($("dbStatusLabel")) {
    $("dbStatusLabel").textContent = engine === "kuzu" ? "Kùzu" : "Neo4j";
  }
}

async function loadSettings() {
  const settings = await latestApi("settings", "/api/settings");
  if (!settings) return;
  const engine = settings.graph_db_engine || "kuzu";
  $("settingsGraphDbEngine").value = engine;
  $("settingsKuzuDbPath").value = settings.kuzu_db_path || "";
  $("settingsKuzuBufferPoolSizeGb").value = settings.kuzu_buffer_pool_size_gb || 1;
  $("settingsNeo4jUri").value = settings.neo4j_uri || "";
  $("settingsNeo4jUser").value = settings.neo4j_user || "";
  toggleEngineSettings(engine);
  if ($("dbStatusLabel")) {
    $("dbStatusLabel").textContent = engine === "kuzu" ? "Kùzu" : "Neo4j";
  }
  $("steamSecretState").textContent = secretLabel(settings.steam_api_key_configured, settings.steam_api_key_from_env);
  $("steamProxyState").textContent = secretLabel(settings.steam_proxy_configured, settings.steam_proxy_from_env);
  $("clearSteamProxy").disabled = !settings.steam_proxy_configured || settings.steam_proxy_from_env;
  $("neo4jSecretState").textContent = secretLabel(settings.neo4j_password_configured, settings.neo4j_password_from_env);
  $("settingsMessage").textContent = settings.message || "";
  const activeProjName = settings.active_project || "default";
  $("activeProjectName").textContent = activeProjName === "default" ? t("project.defaultName") : activeProjName;
  loadProjects().catch(() => {
    const label = activeProjName === "default" ? t("project.defaultName") : escapeHtml(activeProjName);
    $("projectList").innerHTML = `<div class="project-item active" data-project-id="${escapeHtml(activeProjName)}"><span>${label}</span><span class="project-meta">${t("project.loadFailed")}</span></div>`;
  });
}

async function saveSettings() {
  clearFieldErrors(["steamProxyInput", "settingsKuzuDbPath", "settingsKuzuBufferPoolSizeGb", "settingsNeo4jUri", "settingsNeo4jUser"]);
  const engine = $("settingsGraphDbEngine").value;
  const steamProxy = $("steamProxyInput").value.trim();

  if (steamProxy && !isValidProxyUrl(steamProxy)) {
    setFieldError("steamProxyInput", t("validation.proxyUrl"));
    throw new Error(t("validation.fixFields"));
  }
  
  if (engine === "kuzu") {
    if (!$("settingsKuzuDbPath").value.trim()) {
      setFieldError("settingsKuzuDbPath", t("validation.required"));
      throw new Error(t("validation.fixFields"));
    }
    const poolSize = parseInt($("settingsKuzuBufferPoolSizeGb").value);
    if (isNaN(poolSize) || poolSize < 1 || poolSize > 64) {
      setFieldError("settingsKuzuBufferPoolSizeGb", "Must be between 1 and 64 GB");
      throw new Error(t("validation.fixFields"));
    }
  } else {
    if (!$("settingsNeo4jUri").value.trim()) {
      setFieldError("settingsNeo4jUri", t("validation.required"));
      throw new Error(t("validation.fixFields"));
    }
    if (!$("settingsNeo4jUser").value.trim()) {
      setFieldError("settingsNeo4jUser", t("validation.required"));
      throw new Error(t("validation.fixFields"));
    }
  }

  const payload = {
    graph_db_engine: engine,
    kuzu_db_path: $("settingsKuzuDbPath").value.trim(),
    kuzu_buffer_pool_size_gb: parseInt($("settingsKuzuBufferPoolSizeGb").value) || 1,
    neo4j_uri: $("settingsNeo4jUri").value.trim(),
    neo4j_user: $("settingsNeo4jUser").value.trim(),
  };
  await api("/api/settings", { method: "PATCH", body: JSON.stringify(payload) });
  const steamKey = $("steamApiKeyInput").value.trim();
  const neo4jPassword = $("neo4jPasswordInput").value;
  if (steamKey) {
    await api("/api/settings/secrets", { method: "POST", body: JSON.stringify({ name: "steam_api_key", value: steamKey }) });
  }
  if (steamProxy) {
    await api("/api/settings/secrets", { method: "POST", body: JSON.stringify({ name: "steam_proxy_url", value: steamProxy }) });
  }
  if (neo4jPassword) {
    await api("/api/settings/secrets", { method: "POST", body: JSON.stringify({ name: "neo4j_password", value: neo4jPassword }) });
  }
  $("steamApiKeyInput").value = "";
  $("steamProxyInput").value = "";
  $("neo4jPasswordInput").value = "";
  await loadSettings();
  await testSettings({ silent: true });
  toast(t("toast.settingsSaved"));
}

async function clearSteamProxy() {
  await api("/api/settings/secrets/steam_proxy_url", { method: "DELETE" });
  $("steamProxyInput").value = "";
  await loadSettings();
  toast(t("toast.proxyCleared"));
}

async function testSettings({ silent = false } = {}) {
  setStatus("steamStatus", "testing");
  setStatus("neo4jStatus", "testing");
  setConnectionDetail("steamStatusDetail", t("status.testing"));
  setConnectionDetail("neo4jStatusDetail", t("status.testing"));
  const result = await api("/api/settings/test", { method: "POST", body: "{}" });
  setStatus("steamStatus", result.steam_ok ? "ok" : "failed");
  setStatus("neo4jStatus", result.neo4j_ok ? "ok" : "failed");
  setConnectionDetail("steamStatusDetail", result.steam_message, result.steam_ok);
  setConnectionDetail("neo4jStatusDetail", result.neo4j_message, result.neo4j_ok);
  if (!silent) toast(`${result.steam_message} · ${result.neo4j_message}`);
  appendSystemLog(result.steam_ok && result.neo4j_ok ? "info" : "warn", "settings", `${result.steam_message} · ${result.neo4j_message}`);
  await loadDbStats().catch(() => {});
  return result;
}

function validateCrawlPayload() {
  clearFieldErrors(["rootUrl", "maxDepth", "maxNodes", "delayMs", "requestConcurrency", "crawlFriendCountMin", "crawlFriendCountMax", "crawlPriorPoolMinLinks"]);
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
    ["requestConcurrency", 1, 16],
    ["crawlPriorPoolMinLinks", 0, Number.MAX_SAFE_INTEGER],
    ["cacheValidDays", 0, Number.MAX_SAFE_INTEGER],
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

function formatElapsed(seconds) {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

function formatTimeLocal(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleString();
}

function startTimer() {
  crawlStartTime = Date.now();
  $("crawlTimer").style.display = "flex";
  $("crawlUtcTime").textContent = `UTC ${new Date().toISOString().replace("T", " ").slice(0, 19)}`;
  clearInterval(timerInterval);
  timerInterval = setInterval(() => {
    const elapsed = Math.floor((Date.now() - crawlStartTime) / 1000);
    $("elapsedTime").textContent = formatElapsed(elapsed);
    $("crawlUtcTime").textContent = `UTC ${new Date().toISOString().replace("T", " ").slice(0, 19)}`;
  }, 1000);
}

// ── Recent roots ──────────────────────────────────────────────────

function saveRecentRoot(url, name, avatar, id) {
  let roots = [];
  try { roots = JSON.parse(localStorage.getItem("sfm_recent_roots") || "[]"); } catch { /* */ }
  roots = roots.filter(r => r.url !== url);
  roots.unshift({ url, name: name || url, avatar, id: id || "" });
  if (roots.length > 10) roots = roots.slice(0, 10);
  try { localStorage.setItem("sfm_recent_roots", JSON.stringify(roots)); } catch { /* */ }
  renderRecentRoots();
}

function renderRecentRoots() {
  let roots = [];
  try { roots = JSON.parse(localStorage.getItem("sfm_recent_roots") || "[]"); } catch { /* */ }
  const list = $("recentRootsList");
  const count = $("recentRootsCount");
  list.innerHTML = "";
  if (!roots.length) {
    count.textContent = "";
    return;
  }
  count.textContent = ` (${roots.length})`;
  roots.forEach(r => {
    const chip = document.createElement("div");
    chip.className = "recent-root-chip";
    chip.title = r.url;
    chip.innerHTML =
      `<img src="${escapeHtml(r.avatar || '')}" alt="" onerror="this.style.display='none'">` +
      `<div class="chip-info">` +
        `<div class="chip-name">${escapeHtml(r.name || r.id || r.url)}</div>` +
        `<div class="chip-meta"><span>${escapeHtml(r.id || '')}</span></div>` +
        `<div class="chip-url">${escapeHtml(r.url)}</div>` +
      `</div>`;
    chip.addEventListener("click", () => {
      $("rootUrl").value = r.url;
      $("graphRoot").value = r.id || "";
    });
    list.appendChild(chip);
  });
}

// ── Presets ───────────────────────────────────────────────────────

const PRESET_KEY = "sfm_presets";
const LAST_CONFIG_KEY = "sfm_last_config";

function getCurrentConfig() {
  return {
    root_url: $("rootUrl").value,
    max_depth: $("maxDepth").value,
    max_nodes: $("maxNodes").value,
    delay_ms: $("delayMs").value,
    request_concurrency: $("requestConcurrency").value,
    cache_valid_days: $("cacheValidDays").value,
    friend_count_min: $("crawlFriendCountMin").value,
    friend_count_max: $("crawlFriendCountMax").value,
    prior_pool_min_links: $("crawlPriorPoolMinLinks").value,
  };
}

function applyConfig(cfg) {
  if (!cfg) return;
  const fields = ["root_url","max_depth","max_nodes","delay_ms","request_concurrency","cache_valid_days","friend_count_min","friend_count_max","prior_pool_min_links"];
  const ids = ["rootUrl","maxDepth","maxNodes","delayMs","requestConcurrency","cacheValidDays","crawlFriendCountMin","crawlFriendCountMax","crawlPriorPoolMinLinks"];
  fields.forEach((f, i) => { if (cfg[f] !== undefined) $(ids[i]).value = cfg[f]; });
}

function loadPresets() {
  let presets = {};
  try { presets = JSON.parse(localStorage.getItem(PRESET_KEY) || "{}"); } catch { /* */ }
  const sel = $("presetSelect");
  sel.querySelectorAll("option:not(:first-child)").forEach(o => o.remove());
  Object.keys(presets).forEach(name => {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    sel.appendChild(opt);
  });
  sel.value = "";
}

function savePreset() {
  const name = prompt(t("preset.promptName"));
  if (!name || !name.trim()) return;
  const cfg = getCurrentConfig();
  let presets = {};
  try { presets = JSON.parse(localStorage.getItem(PRESET_KEY) || "{}"); } catch { /* */ }
  presets[name.trim()] = cfg;
  localStorage.setItem(PRESET_KEY, JSON.stringify(presets));
  loadPresets();
  toast(t("preset.saved"));
}

function applyPreset(name) {
  if (!name) return;
  let presets = {};
  try { presets = JSON.parse(localStorage.getItem(PRESET_KEY) || "{}"); } catch { /* */ }
  const cfg = presets[name];
  if (cfg) { applyConfig(cfg); toast(t("preset.applied", { name })); }
}

function deletePreset() {
  const name = $("presetSelect").value;
  if (!name) return;
  let presets = {};
  try { presets = JSON.parse(localStorage.getItem(PRESET_KEY) || "{}"); } catch { /* */ }
  delete presets[name];
  localStorage.setItem(PRESET_KEY, JSON.stringify(presets));
  loadPresets();
  toast(t("preset.deleted"));
}

function autoSaveLastConfig() {
  try { localStorage.setItem(LAST_CONFIG_KEY, JSON.stringify(getCurrentConfig())); } catch { /* */ }
}

function autoLoadLastConfig() {
  let cfg = null;
  try { cfg = JSON.parse(localStorage.getItem(LAST_CONFIG_KEY)); } catch { /* */ }
  if (cfg) applyConfig(cfg);
}

function stopTimer() {
  clearInterval(timerInterval);
  timerInterval = null;
  if (crawlStartTime) {
    const elapsed = Math.floor((Date.now() - crawlStartTime) / 1000);
    $("elapsedTime").textContent = formatElapsed(elapsed);
  }
  crawlStartTime = null;
}

async function startCrawl() {
  if (!validateCrawlPayload()) throw new Error(t("validation.fixFields"));
  const payload = {
    root_url: $("rootUrl").value.trim(),
    max_depth: Number($("maxDepth").value || 2),
    max_nodes: Number($("maxNodes").value || 2000),
    delay_ms: Number($("delayMs").value || 300),
    request_concurrency: Number($("requestConcurrency").value || 4),
    prior_pool_min_links: Number($("crawlPriorPoolMinLinks").value || 0),
    cache_valid_days: Number($("cacheValidDays").value === "" ? 14 : $("cacheValidDays").value),
  };
  const friendMin = $("crawlFriendCountMin").value.trim();
  const friendMax = $("crawlFriendCountMax").value.trim();
  if (friendMin) payload.friend_count_min = Number(friendMin);
  if (friendMax) payload.friend_count_max = Number(friendMax);
  const run = await api("/api/crawls", { method: "POST", body: JSON.stringify(payload) });
  invalidateNetworkAnalysis(false);
  currentRunId = run.id;
  saveRecentRoot(payload.root_url, run.root_steam_id, "", run.root_steam_id);
  startDbStatsPolling();
  lastEventSeq = 0;
  $("crawlLogs").innerHTML = "";
  setProgress(1);
  $("graphRoot").value = run.root_steam_id;
  $("analysisRoot").value = run.root_steam_id;
  toast(t("toast.crawlStarted"));
  appendSystemLog("info", "crawl", t("toast.crawlStarted"));
  startTimer();
  pollRun();
}

async function pollRun() {
  if (!currentRunId) return;
  clearTimeout(pollTimer);
  const run = await api(`/api/crawls/${currentRunId}`);
  setStatus("crawlStatus", run.status);
  updateCrawlButtons(run.status);
  $("nodeCount").textContent = run.nodes_discovered;
  $("edgeCount").textContent = run.edges_discovered;
  $("privateCount").textContent = run.private_count;
  $("filteredCount").textContent = run.filtered_count || 0;
  setProgress(run.progress_percent);
  if (run.last_event) $("lastEvent").textContent = run.last_event;
  await loadEvents().catch(() => {});
  if (["completed", "cancelled", "stopped", "failed"].includes(run.status)) {
    stopTimer();
    stopDbStatsPolling();
    updateCrawlButtons(run.status);
    toast(run.message || statusText(run.status));
    appendSystemLog(run.status === "failed" ? "error" : "info", "crawl", run.message || statusText(run.status));
    invalidateNetworkAnalysis(false);
    await loadGraph().catch(() => {});
    await loadDbStats().catch(() => {});
    // 更新最近扫描的 Root 头像和昵称
    if (currentGraph.nodes.length) {
      const rootNode = currentGraph.nodes.find(n => n.id === run.root_steam_id);
      if (rootNode) {
        saveRecentRoot($("rootUrl").value || rootNode.profile_url, rootNode.label, rootNode.avatar, rootNode.id);
      }
    }
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
  if (!currentRunId) { toast(t("toast.noActiveCrawl")); return; }
  await api(`/api/crawls/${currentRunId}/cancel`, { method: "POST", body: "{}" });
  toast(t("toast.cancelRequested"));
}

async function forceStopCrawl() {
  if (!currentRunId) { toast(t("toast.noActiveCrawl")); return; }
  await api(`/api/crawls/${currentRunId}/force-stop`, { method: "POST", body: "{}" });
  stopTimer();
  stopDbStatsPolling();
  toast(t("toast.forceStop"));
}

async function pauseCrawl() {
  if (!currentRunId) return;
  await api(`/api/crawls/${currentRunId}/pause`, { method: "POST", body: "{}" });
  $("pauseCrawl").style.display = "none";
  $("resumeCrawl").style.display = "";
  toast(t("toast.paused"));
}

async function resumeCrawl() {
  if (!currentRunId) return;
  await api(`/api/crawls/${currentRunId}/resume`, { method: "POST", body: "{}" });
  $("pauseCrawl").style.display = "";
  $("resumeCrawl").style.display = "none";
  toast(t("toast.resumed"));
}

function updateCrawlButtons(status) {
  const running = status === "running";
  const paused = status === "paused";
  const active = running || paused;
  $("cancelCrawl").style.display = active ? "" : "none";
  $("forceStopCrawl").style.display = active ? "" : "none";
  $("pauseCrawl").style.display = running ? "" : "none";
  $("resumeCrawl").style.display = paused ? "" : "none";
}

async function loadProjects() {
  try {
    const data = await latestApi("projects", "/api/projects");
    if (!data) return;
    const activeProjId = data.active_project_id || "default";
    $("activeProjectName").textContent = activeProjId === "default" ? t("project.defaultName") : activeProjId;
    renderProjectList(data);
  } catch (error) {
    const activeProjId = $("activeProjectName").textContent.trim() || "default";
    const message = error.message.includes("buffer pool is full") ? t("graph.memoryHint") : error.message;
    appendUiLog("error", t("project.loadFailed"), message);
    $("projectList").innerHTML = `<div class="project-item active" data-project-id="${escapeHtml(activeProjId)}"><span>${escapeHtml(activeProjId)}</span><span class="project-meta">${t("project.loadFailed")}</span></div>`;
    throw error;
  }
}

function renderProjectList(data) {
  const list = $("projectList");
  list.innerHTML = data.projects
    .map(
      (p) => `
    <div class="project-item${p.id === data.active_project_id ? " active" : ""}" data-project-id="${escapeHtml(p.id)}">
      <div class="project-item-header">
        <span class="project-name">${p.id === "default" ? t("project.defaultName") : escapeHtml(p.name)}</span>
        ${p.id !== "default" ? `<button class="icon-button mini danger delete-project" data-project-id="${escapeHtml(p.id)}" title="${t("action.deleteProject")}"><i data-lucide="trash-2"></i></button>` : ""}
      </div>
      <span class="project-meta">${p.steam_users} ${t("metric.nodes")} · ${p.relationships} ${t("metric.edges")} · ${p.crawl_runs} ${t("project.crawls")}</span>
    </div>`,
    )
    .join("");

  // Wire click to switch
  list.querySelectorAll(".project-item").forEach((item) => {
    item.addEventListener("click", async (e) => {
      if (e.target.closest(".delete-project")) return;
      const pid = item.dataset.projectId;
      if (pid === data.active_project_id) return;
      await withButtonState(item, async () => {
        cancelProjectScopedRequests();
        await api("/api/projects/switch", { method: "POST", body: JSON.stringify({ project_id: pid }) });
        resetProjectScopedViews();
        await loadSettings();
        await loadDbStats().catch(() => {});
        await loadGraph().catch(() => {});
        toast(t("toast.projectSwitched"));
      });
    });
  });

  // Wire delete buttons
  list.querySelectorAll(".delete-project").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const pid = btn.dataset.projectId;
      if (!confirm(t("project.confirmDelete", { name: pid }))) return;
      await withButtonState(btn, async () => {
        cancelProjectScopedRequests();
        await api(`/api/projects/${pid}`, { method: "DELETE" });
        if (pid === data.active_project_id) resetProjectScopedViews();
        await loadSettings();
        await loadProjects();
        await loadDbStats().catch(() => {});
        await loadGraph().catch(() => {});
        toast(t("toast.projectDeleted"));
      });
    });
  });

  if (window.lucide) window.lucide.createIcons();
}

async function createProject() {
  const name = $("newProjectName").value.trim();
  if (!name) {
    toast(t("validation.projectNameRequired"));
    return;
  }
  await api("/api/projects", { method: "POST", body: JSON.stringify({ name }) });
  $("newProjectName").value = "";
  await loadProjects();
  toast(t("toast.projectCreated"));
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
  const data = await latestApi("graph", `/api/path?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}&max_depth=4`);
  if (!data) return;
  if (!data.nodes.length) {
    $("pathResult").dataset.state = "no-path";
    $("pathResult").textContent = t("path.noPath");
    return;
  }
  $("pathResult").dataset.state = "path";
  renderGraph(data);
  $("pathResult").textContent = data.nodes.map((node) => node.label || node.id).join(" -> ");
}

function formatPageRank(value) {
  const numeric = Number(value || 0) * 100;
  return `${numeric.toFixed(numeric < 0.01 ? 4 : 3)}%`;
}

function networkCommunityColor(community) {
  return COMMUNITY_COLORS[(Math.max(1, Number(community)) - 1) % COMMUNITY_COLORS.length];
}

function invalidateNetworkAnalysis(rerender = true) {
  currentNetworkAnalysis = null;
  if ($("networkCommunityCount")) $("networkCommunityCount").textContent = "-";
  if ($("networkModularity")) $("networkModularity").textContent = "-";
  if ($("networkAnalyzed")) $("networkAnalyzed").textContent = "-";
  if ($("networkLeaderList")) {
    $("networkLeaderList").innerHTML = `<li class="rank-empty">${escapeHtml(t("analysis.networkIdle"))}</li>`;
  }
  if (rerender && cy && currentGraph.nodes.length) renderGraph(currentGraph);
  if (selectedNode) fillProfile(selectedNode);
}

function resetProjectScopedViews() {
  cancelProjectScopedRequests();
  graphLifecycle.cancel();
  currentGraph = { nodes: [], edges: [], limited: false };
  currentNetworkAnalysis = null;
  selectedNode = null;

  if (cy) cy.elements().remove();
  updateGraphSummary();
  $("graphRoot").value = "";
  $("analysisRoot").value = "";
  $("graphEmpty").classList.remove("hidden");
  const emptyHint = $("graphEmpty").querySelector("p");
  if (emptyHint) emptyHint.textContent = t("graph.emptyHint");
  $("graphLoading")?.classList.add("hidden");

  invalidateNetworkAnalysis(false);
  $("friendCircleList").innerHTML = "";
  $("pathResult").dataset.state = "empty";
  $("pathResult").textContent = t("path.empty");
  $("pathFrom").value = "";
  $("pathTo").value = "";
  fillProfile({
    id: "",
    label: t("profile.empty"),
    avatar: "",
    profile_url: "",
    friend_list_status: "unknown",
    tags: [],
  });
  selectedNode = null;
}

function renderNetworkAnalysisResults(data) {
  $("networkCommunityCount").textContent = data.community_count;
  $("networkModularity").textContent = Number(data.modularity || 0).toFixed(3);
  $("networkAnalyzed").textContent = t("analysis.analyzedValue", {
    nodes: data.analyzed_nodes,
    edges: data.analyzed_edges,
  });
  $("networkLeaderList").innerHTML = data.leaders.length
    ? data.leaders.map((leader) => {
      const color = networkCommunityColor(leader.community);
      const detail = t("analysis.networkRow", {
        pagerank: formatPageRank(leader.pagerank),
        community: leader.community,
        degree: leader.degree,
      });
      return `<li><button class="rank-button" data-network-id="${escapeHtml(leader.id)}"><span class="rank-title"><span class="community-swatch" style="--community-color: ${color}"></span><strong>${escapeHtml(leader.label)}</strong></span><span>${escapeHtml(detail)}</span></button></li>`;
    }).join("")
    : `<li class="rank-empty">${escapeHtml(t("analysis.networkEmpty"))}</li>`;
  $("networkLeaderList").querySelectorAll(".rank-button").forEach((button) => {
    button.addEventListener("click", () => focusNetworkLeader(button.dataset.networkId));
  });
}

async function loadNetworkAnalysis(options = {}) {
  const data = await latestApi("network-analysis", "/api/analysis/network?limit=12");
  if (!data) return;
  currentNetworkAnalysis = data;
  renderNetworkAnalysisResults(data);
  if (currentGraph.nodes.length) renderGraph(currentGraph);
  if (selectedNode) fillProfile(selectedNode);
  if (!options.silent) toast(t("toast.networkAnalysisLoaded"));
}

function focusNetworkLeader(steamId) {
  const leader = currentNetworkAnalysis?.leaders.find((item) => item.id === steamId);
  cy.elements().removeClass("analysis-focus analysis-evidence");
  const node = cy.getElementById(steamId);
  if (node.length) {
    node.addClass("analysis-focus");
    cy.center(node);
    fillProfile(node.data().node);
  } else if (leader) {
    fillProfile({
      id: leader.id,
      label: leader.label,
      avatar: leader.avatar,
      profile_url: leader.profile_url,
      degree: leader.degree,
      friend_list_status: "unknown",
      note: "",
      tags: [],
      category: "",
    });
  }
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
  const data = await latestApi("friend-circles", `/api/analysis/friend-circles?${params.toString()}`);
  if (!data) return;
  $("friendCircleList").innerHTML = data.candidates
    .map(
      (item) =>
        `<li><button class="rank-button" data-steam-id="${escapeHtml(item.steam_id)}"><strong>${escapeHtml(item.label)}</strong><span>${escapeHtml(t("analysis.row", {
          mutual: item.mutual_count,
          score: item.score,
        }))}</span></button></li>`,
    )
    .join("");
  $("friendCircleList").querySelectorAll(".rank-button").forEach((button) => {
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
    fillProfile(node.data().node);
  } else if (candidate) {
    fillProfile({
      id: candidate.steam_id,
      label: candidate.label,
      avatar: candidate.avatar,
      profile_url: candidate.profile_url,
      friend_count: candidate.friend_count,
      depth_min: candidate.depth,
      friend_list_status: "unknown",
      friend_count_status: "unknown",
      note: "",
      tags: [],
      category: ""
    });
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
    if (!response.ok) {
      let message = `${response.status} ${response.statusText}`.trim();
      try {
        const payload = await response.json();
        message = payload.detail || message;
      } catch {
        // Keep the HTTP status when the server did not return JSON.
      }
      throw new Error(message);
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `steam_graph.${format}`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 0);
  } catch (err) {
    toast(t("toast.exportFailed", { message: err.message }));
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
  // SteamID 智能链接解析提取
  $("graphRoot").addEventListener("input", (event) => {
    const val = event.target.value.trim();
    const profileMatch = val.match(/profiles\/([0-9]{17})/);
    const idMatch = val.match(/id\/([a-zA-Z0-9_-]+)/);
    if (profileMatch) {
      event.target.value = profileMatch[1];
      autoSaveLastConfig();
    } else if (idMatch) {
      event.target.value = idMatch[1];
      autoSaveLastConfig();
    }
  });

  // 一键重置筛选
  $("resetFilters").addEventListener("click", () => {
    $("graphDepth").value = "2";
    $("graphLimit").value = "500";
    $("graphLimit").max = "2000";
    $("graphLimit").disabled = false;
    
    const limitToggle = $("graphLimitToggle");
    if (limitToggle) {
      limitToggle.checked = false;
      limitToggle.dispatchEvent(new Event("change"));
    }
    
    $("graphSearch").value = "";
    $("graphCategory").value = "";
    $("graphFriendCountMin").value = "";
    $("graphFriendCountMax").value = "";
    $("graphPriorPoolMinLinks").value = "0";
    $("graphSortBy").value = "depth";
    $("graphSortDir").value = "asc";
    
    autoSaveLastConfig();
    const emptyMsg = $("graphEmpty") ? $("graphEmpty").querySelector("p") : null;
    if (emptyMsg) emptyMsg.textContent = t("graph.emptyHint");
    loadGraph().catch(() => {});
    toast(t("toast.filtersReset"));
  });

  document.querySelectorAll(".lang-button").forEach((button) => {
    button.addEventListener("click", () => setLanguage(button.dataset.lang));
  });
  $("settingsGraphDbEngine").addEventListener("change", (event) => toggleEngineSettings(event.target.value));
  $("testSettings").addEventListener("click", (event) => withButtonState(event.currentTarget, testSettings).catch(() => {}));
  $("loadSettings").addEventListener("click", (event) => withButtonState(event.currentTarget, loadSettings).catch(() => {}));
  $("saveSettings").addEventListener("click", (event) => withButtonState(event.currentTarget, saveSettings).catch(() => {}));
  $("clearSteamProxy").addEventListener("click", (event) => withButtonState(event.currentTarget, clearSteamProxy).catch(() => {}));
  $("refreshDbStats").addEventListener("click", (event) => withButtonState(event.currentTarget, loadDbStats).catch(() => {}));
  $("dbStatsInterval").addEventListener("change", () => {
    if (currentRunId && ["running", "paused"].includes($("crawlStatus").dataset.status || "")) {
      startDbStatsPolling();
    }
  });
  $("startCrawl").addEventListener("click", (event) => withButtonState(event.currentTarget, startCrawl).catch(() => {}));
  $("cancelCrawl").addEventListener("click", (event) => withButtonState(event.currentTarget, cancelCrawl).catch(() => {}));
  $("forceStopCrawl").addEventListener("click", (event) => withButtonState(event.currentTarget, forceStopCrawl).catch(() => {}));
  $("pauseCrawl").addEventListener("click", (event) => withButtonState(event.currentTarget, pauseCrawl).catch(() => {}));
  $("resumeCrawl").addEventListener("click", (event) => withButtonState(event.currentTarget, resumeCrawl).catch(() => {}));
  $("refreshGraph").addEventListener("click", (event) => withButtonState(event.currentTarget, loadGraph).catch(() => {}));
  $("fitGraph").addEventListener("click", (event) => withButtonState(event.currentTarget, async () => cy.fit(undefined, 40)).catch(() => {}));
  $("layoutGraph").addEventListener("click", (event) => withButtonState(event.currentTarget, async () => runLayout()).catch(() => {}));
  $("saveProfile").addEventListener("click", (event) => withButtonState(event.currentTarget, saveProfile).catch(() => {}));
  $("findPath").addEventListener("click", (event) => withButtonState(event.currentTarget, findPath).catch(() => {}));
  $("loadNetworkAnalysis").addEventListener("click", (event) => withButtonState(event.currentTarget, loadNetworkAnalysis).catch(() => {}));
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
  const savedCommunityColors = localStorage.getItem("sfm_community_colors");
  if (savedCommunityColors !== null) $("communityColors").checked = savedCommunityColors === "true";
  $("communityColors").addEventListener("change", () => {
    localStorage.setItem("sfm_community_colors", String($("communityColors").checked));
    renderGraph(currentGraph);
  });
  
  // Graph Limit Toggle logic
  const limitInput = $("graphLimit");
  const limitToggle = $("graphLimitToggle");
  const limitBtn = $("graphLimitBtn");
  if (limitInput && limitToggle) {
    const savedNoLimit = localStorage.getItem("sfm_no_limit") === "true";
    limitToggle.checked = savedNoLimit;
    limitInput.max = savedNoLimit ? 100000 : 2000;

    const syncLimitBtn = () => {
      if (!limitBtn) return;
      const isChecked = limitToggle.checked;
      limitBtn.classList.toggle("active", isChecked);
      const icon = limitBtn.querySelector('i, svg');
      if (icon) {
        if (isChecked) {
          icon.setAttribute("data-lucide", "unlock");
        } else {
          icon.setAttribute("data-lucide", "lock");
        }
        if (window.lucide) window.lucide.createIcons();
      }
    };

    syncLimitBtn();
    
    limitToggle.addEventListener("change", () => {
      const isChecked = limitToggle.checked;
      localStorage.setItem("sfm_no_limit", isChecked);
      limitInput.max = isChecked ? 100000 : 2000;
      if (!isChecked && Number(limitInput.value) > 2000) {
        limitInput.value = 2000;
      }
      syncLimitBtn();
    });

    if (limitBtn) {
      limitBtn.addEventListener("click", () => {
        limitToggle.click();
      });
    }
  }
  $("exportJson").addEventListener("click", (event) => withButtonState(event.currentTarget, async () => exportFile("json")).catch(() => {}));
  $("exportCsv").addEventListener("click", (event) => withButtonState(event.currentTarget, async () => exportFile("csv")).catch(() => {}));
  $("copyBloom").addEventListener("click", (event) =>
    withButtonState(event.currentTarget, async () => {
      await navigator.clipboard.writeText($("bloomQuery").value);
      toast(t("toast.copied"));
    }).catch(() => {}),
  );
  $("refreshProjects").addEventListener("click", (event) => withButtonState(event.currentTarget, loadProjects).catch(() => {}));
  $("createProject").addEventListener("click", (event) => withButtonState(event.currentTarget, createProject).catch(() => {}));
  $("newProjectName").addEventListener("keydown", (e) => {
    if (e.key === "Enter") withButtonState("createProject", createProject).catch(() => {});
  });
  $("themeToggle").addEventListener("click", cycleTheme);
  $("toggleConsole").addEventListener("click", () => {
    if (window._toggleConsole) window._toggleConsole();
  });
  // Sidebar Tabs switching
  const sidebarSlider = $("sidebarTabSlider");
  const sidebarButtons = document.querySelectorAll(".tab-button");
  sidebarButtons.forEach((button, idx) => {
    button.addEventListener("click", () => {
      sidebarButtons.forEach((b) => b.classList.remove("active"));
      button.classList.add("active");
      if (sidebarSlider) {
        sidebarSlider.dataset.activeIndex = idx;
      }
    });
  });
  // Inspector Tabs switching
  const inspectorSlider = $("inspectorTabSlider");
  const inspectorButtons = document.querySelectorAll(".ins-tab-button");
  inspectorButtons.forEach((button, idx) => {
    button.addEventListener("click", () => {
      inspectorButtons.forEach((b) => b.classList.remove("active"));
      button.classList.add("active");
      if (inspectorSlider) {
        inspectorSlider.dataset.activeIndex = idx;
      }
      if (button.dataset.target === "insTabRank" && !currentNetworkAnalysis) {
        withButtonState("loadNetworkAnalysis", () => loadNetworkAnalysis({ silent: true })).catch(() => {});
      }
    });
  });
  // Presets
  $("presetSelect").addEventListener("change", () => applyPreset($("presetSelect").value));
  $("savePreset").addEventListener("click", savePreset);
  $("deletePreset").addEventListener("click", deletePreset);
  // Crawl logs local filtering and clearing
  if ($("crawlLogLevel")) {
    $("crawlLogLevel").addEventListener("change", () => {
      const selectedLevel = $("crawlLogLevel").value;
      document.querySelectorAll("#crawlLogs .log-item").forEach((row) => {
        if (!selectedLevel) {
          row.style.display = "";
        } else {
          row.style.display = row.dataset.level === selectedLevel ? "" : "none";
        }
      });
    });
  }
  if ($("clearCrawlLogs")) {
    $("clearCrawlLogs").addEventListener("click", () => {
      $("crawlLogs").innerHTML = "";
      toast(t("toast.logsCleared"));
    });
  }
  // Auto-save last config on any crawl input change
  ["rootUrl","maxDepth","maxNodes","delayMs","cacheValidDays","crawlFriendCountMin","crawlFriendCountMax","crawlPriorPoolMinLinks"].forEach(id => {
    $(id).addEventListener("change", autoSaveLastConfig);
    $(id).addEventListener("input", autoSaveLastConfig);
  });
}

// ── Resizable panels ──────────────────────────────────────────────

function initResizeHandles() {
  const shell = document.querySelector(".app-shell");
  const leftHandle = $("resizeHandleLeft");
  const rightHandle = $("resizeHandleRight");
  if (!shell || !leftHandle || !rightHandle) return;

  const STORAGE_KEY = "sfm_panel_sizes";
  let saved = null;
  try {
    saved = JSON.parse(localStorage.getItem(STORAGE_KEY));
  } catch { /* ignore */ }

  // 仅在用户拖拽过时才用 px 覆盖 CSS 的 fr 比例
  if (saved) {
    shell.style.gridTemplateColumns = `${saved.left}px 6px minmax(280px, 1fr) 6px ${saved.right}px`;
  }

  function persist(left, right) {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify({ left, right })); } catch { /* ignore */ }
  }

  function makeDraggable(handle, side) {
    handle.addEventListener("mousedown", (e) => {
      e.preventDefault();
      const startX = e.clientX;
      const isLeft = side === "left";
      // 首次拖拽：从当前 fr 布局的 computed width 读取实际 px 值
      const panel = isLeft
        ? document.querySelector(".sidebar")
        : document.querySelector(".inspector");
      const startSize = panel ? panel.getBoundingClientRect().width : (isLeft ? 320 : 340);
      const shellW = shell.getBoundingClientRect().width;

      document.body.classList.add("resize-in-progress");
      handle.classList.add("active");

      function onMove(ev) {
        const delta = ev.clientX - startX;
        let newSize = isLeft ? startSize + delta : startSize - delta;
        const minW = 220;
        const maxW = Math.floor(shellW - 360);
        newSize = Math.max(minW, Math.min(newSize, Math.max(minW, maxW)));
        // 实时更新：当前拖拽侧用 px，另一侧保持原状
        const otherPanel = isLeft
          ? document.querySelector(".inspector")
          : document.querySelector(".sidebar");
        const otherW = otherPanel ? otherPanel.getBoundingClientRect().width : 340;
        const leftW = isLeft ? newSize : otherW;
        const rightW = isLeft ? otherW : newSize;
        shell.style.gridTemplateColumns = `${leftW}px 6px minmax(280px, 1fr) 6px ${rightW}px`;
      }

      function onUp() {
        document.body.classList.remove("resize-in-progress");
        handle.classList.remove("active");
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        // 保存当前两栏的 px 宽度
        const leftPanel = document.querySelector(".sidebar");
        const rightPanel = document.querySelector(".inspector");
        persist(
          leftPanel ? leftPanel.getBoundingClientRect().width : 320,
          rightPanel ? rightPanel.getBoundingClientRect().width : 340,
        );
      }

      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    });
  }

  makeDraggable(leftHandle, "left");
  makeDraggable(rightHandle, "right");

  // 双击 → 清除保存 → 恢复 CSS 默认 fr 比例
  function resetToRatio() {
    shell.style.gridTemplateColumns = "";
    try { localStorage.removeItem(STORAGE_KEY); } catch { /* ignore */ }
  }
  leftHandle.addEventListener("dblclick", resetToRatio);
  rightHandle.addEventListener("dblclick", resetToRatio);
}

// ── Console panel ─────────────────────────────────────────────────

function initConsole() {
  const panel = $("consolePanel");
  const handle = $("consoleResizeHandle");
  if (!panel || !handle) return;

  const STORAGE_KEY = "sfm_console";
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(STORAGE_KEY)); } catch { /* ignore */ }

  const state = { open: saved?.open ?? true, height: saved?.height ?? 220 };

  function persist() {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(state)); } catch { /* ignore */ }
  }

  function apply() {
    const root = document.documentElement;
    if (state.open) {
      panel.classList.remove("collapsed");
      handle.style.display = "";
      root.style.setProperty("--console-height", state.height + "px");
    } else {
      panel.classList.add("collapsed");
      handle.style.display = "none";
      root.style.setProperty("--console-height", "0px");
    }
  }

  apply();

  window._toggleConsole = function () {
    state.open = !state.open;
    if (!state.open && state.height < 60) state.height = 220;
    apply();
    persist();
  };

  handle.addEventListener("mousedown", (e) => {
    if (!state.open) return;
    e.preventDefault();
    const startY = e.clientY;
    const startH = state.height;

    document.body.classList.add("resize-in-progress");
    handle.classList.add("active");

    function onMove(ev) {
      const delta = startY - ev.clientY;
      const newH = Math.max(60, Math.min(startH + delta, window.innerHeight * 0.6));
      state.height = newH;
      document.documentElement.style.setProperty("--console-height", newH + "px");
    }

    function onUp() {
      document.body.classList.remove("resize-in-progress");
      handle.classList.remove("active");
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      persist();
    }

    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });
}

window.addEventListener("error", (event) => {
  appendSystemLog("error", "frontend", event.message);
});

window.addEventListener("unhandledrejection", (event) => {
  appendSystemLog("error", "frontend", event.reason?.message || String(event.reason));
});

document.addEventListener("DOMContentLoaded", async () => {
  initTheme();
  await loadI18n();
  applyTranslations();
  if (window.lucide) window.lucide.createIcons();
  initGraph();
  initResizeHandles();
  initConsole();
  renderRecentRoots();
  loadPresets();
  autoLoadLastConfig();
  wireEvents();
  initMagneticButtons();
  initHapticFeedback();
  $("pathResult").dataset.state = "empty";
  loadSettings()
    .then(() => testSettings({ silent: true }))
    .catch((error) => appendSystemLog("error", "settings", error.message));
  loadGraph().catch(() => {});
  loadDbStats().catch((error) => appendSystemLog("error", "db", error.message));
  loadSystemLogs(true).catch(() => {});
  startSystemLogPolling();
});

// ── Apple Ecosystem Taptic / Haptic Feedback Scheme ─────────────────
const Haptic = {
  // Trigger physical vibration (supports natively injected bridges & Web Vibration API)
  trigger(style = 'light') {
    if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.haptic) {
      window.webkit.messageHandlers.haptic.postMessage({ style });
      return;
    }
    if (window.__TAURI__ && window.__TAURI__.invoke) {
      window.__TAURI__.invoke('plugin:haptics|trigger', { style }).catch(() => {});
      return;
    }
    
    if (navigator.vibrate) {
      try {
        switch (style) {
          case 'light': navigator.vibrate(10); break;
          case 'medium': navigator.vibrate(20); break;
          case 'heavy': navigator.vibrate(40); break;
          case 'success': navigator.vibrate([10, 30, 10]); break;
          case 'warning': navigator.vibrate([20, 50, 20]); break;
          case 'error': navigator.vibrate([30, 80, 30]); break;
        }
      } catch (e) {}
    }
  },
  
  // Visual Micro-Haptic Click (simulates Apple physical tactile key deformation)
  visualClick(element) {
    if (!element) return;
    const originalTransform = element.style.transform || '';
    const baseTransform = originalTransform.replace(/scale\([0-9.]+\)/g, '').trim();
    element.style.transition = 'transform 0.05s cubic-bezier(0.25, 1, 0.5, 1)';
    element.style.transform = `${baseTransform} scale(0.95)`.trim();
    
    setTimeout(() => {
      element.style.transition = 'transform 0.4s cubic-bezier(0.34, 1.56, 0.64, 1)';
      element.style.transform = baseTransform;
    }, 60);
  }
};

function initHapticFeedback() {
  const clickSelector = '.primary, .secondary, .icon-button, .tool, .theme-toggle, .tab-button, .ins-tab-button, .lang-button';
  
  document.addEventListener('pointerdown', (e) => {
    const btn = e.target.closest(clickSelector);
    if (btn && !btn.disabled && !btn.classList.contains('is-loading')) {
      Haptic.trigger('light');
      Haptic.visualClick(btn);
    }
  });
}

function initMagneticButtons() {
  const selector = '.primary, .secondary, .icon-button, .tool, .theme-toggle';
  
  document.addEventListener('mousemove', (e) => {
    const btn = e.target.closest(selector);
    if (!btn || btn.disabled || btn.classList.contains('is-loading')) {
      clearActiveMagneticBtn();
      return;
    }
    
    if (window.activeMagneticBtn !== btn) {
      clearActiveMagneticBtn();
      window.activeMagneticBtn = btn;
    }
    
    const rect = btn.getBoundingClientRect();
    const x = e.clientX - rect.left - rect.width / 2;
    const y = e.clientY - rect.top - rect.height / 2;
    
    const isSmall = !btn.classList.contains('primary') && !btn.classList.contains('secondary');
    const maxDelta = isSmall ? 3 : 4;
    const strength = 0.12;
    
    let moveX = x * strength;
    let moveY = y * strength;
    
    moveX = Math.max(-maxDelta, Math.min(maxDelta, moveX));
    moveY = Math.max(-maxDelta, Math.min(maxDelta, moveY));
    
    btn.style.transition = 'transform 0.1s cubic-bezier(0.25, 1, 0.5, 1), background-color 0.25s, border-color 0.25s, box-shadow 0.25s';
    const scale = isSmall ? 1.03 : 1.008;
    btn.style.transform = `translate3d(${moveX}px, ${moveY}px, 0) scale(${scale})`;
    
    const icon = btn.querySelector('svg, [data-lucide]');
    if (icon) {
      const rotateDeg = x * 0.03;
      icon.style.transition = 'transform 0.1s ease-out';
      icon.style.transform = `rotate(${Math.max(-6, Math.min(6, rotateDeg))}deg)`;
    }
  });
  
  document.addEventListener('mouseout', (e) => {
    const btn = e.target.closest(selector);
    if (btn && window.activeMagneticBtn === btn) {
      if (!e.relatedTarget || !btn.contains(e.relatedTarget)) {
        clearActiveMagneticBtn();
      }
    }
  });
  
  function clearActiveMagneticBtn() {
    if (window.activeMagneticBtn) {
      const btn = window.activeMagneticBtn;
      btn.style.transition = 'transform 0.4s cubic-bezier(0.34, 1.56, 0.64, 1), background-color 0.25s, border-color 0.25s, box-shadow 0.25s';
      btn.style.transform = 'translate3d(0, 0, 0)';
      
      const icon = btn.querySelector('svg, [data-lucide]');
      if (icon) {
        icon.style.transition = 'transform 0.4s cubic-bezier(0.34, 1.56, 0.64, 1)';
        icon.style.transform = 'rotate(0deg)';
      }
      window.activeMagneticBtn = null;
    }
  }
}
