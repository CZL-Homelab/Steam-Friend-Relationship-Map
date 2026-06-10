const $ = (id) => document.getElementById(id);

let cy;
let currentRunId = null;
let pollTimer = null;
let selectedNode = null;

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
    selectedNode = event.target.data();
    fillProfile(selectedNode);
  });
}

function renderGraph(data) {
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
  $("graphSummary").textContent = `${data.nodes.length} nodes · ${data.edges.length} edges${data.limited ? " · limited" : ""}`;
}

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

function fillProfile(data) {
  const node = data.node || data;
  selectedNode = node;
  $("profileAvatar").src = node.avatar || "";
  $("profileName").textContent = node.label || "Unknown";
  $("profileUrl").href = node.profile_url || "#";
  $("profileUrl").textContent = node.profile_url || "Steam profile";
  $("profileSteamId").textContent = node.id || "-";
  $("profileDegree").textContent = node.degree ?? 0;
  $("profileStatus").textContent = node.friend_list_status || "unknown";
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
  $("steamStatus").textContent = "Testing";
  $("neo4jStatus").textContent = "Testing";
  const result = await api("/api/settings/test", { method: "POST", body: "{}" });
  $("steamStatus").textContent = result.steam_ok ? "OK" : "Failed";
  $("neo4jStatus").textContent = result.neo4j_ok ? "OK" : "Failed";
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
    toast("Root URL is required");
    return;
  }
  const run = await api("/api/crawls", { method: "POST", body: JSON.stringify(payload) });
  currentRunId = run.id;
  $("graphRoot").value = run.root_steam_id;
  toast("Crawl started");
  pollRun();
}

async function pollRun() {
  if (!currentRunId) return;
  clearTimeout(pollTimer);
  const run = await api(`/api/crawls/${currentRunId}`);
  $("crawlStatus").textContent = run.status;
  $("nodeCount").textContent = run.nodes_discovered;
  $("edgeCount").textContent = run.edges_discovered;
  $("privateCount").textContent = run.private_count;
  if (["completed", "cancelled", "failed"].includes(run.status)) {
    toast(run.message || run.status);
    await loadGraph().catch(() => {});
    return;
  }
  pollTimer = setTimeout(pollRun, 1200);
}

async function cancelCrawl() {
  if (!currentRunId) return;
  await api(`/api/crawls/${currentRunId}/cancel`, { method: "POST", body: "{}" });
  toast("Cancel requested");
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
  toast("Profile saved");
  await loadGraph();
}

async function findPath() {
  const from = $("pathFrom").value.trim();
  const to = $("pathTo").value.trim();
  if (!from || !to) {
    toast("From and To are required");
    return;
  }
  const data = await api(`/api/path?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}&max_depth=4`);
  if (!data.nodes.length) {
    $("pathResult").textContent = "No path";
    return;
  }
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
    toast("Copied");
  });
}

document.addEventListener("DOMContentLoaded", () => {
  if (window.lucide) window.lucide.createIcons();
  initGraph();
  wireEvents();
  loadGraph().catch(() => {});
  loadTopDegree().catch(() => {});
});
