from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC_DIR = Path("src/steam_friend_relationship_map/static")
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_graph_collision_separates_chains_and_keeps_dragged_root_anchored() -> None:
    module_path = str((STATIC_DIR / "graph-collision.js").resolve())
    script = f"""
      const {{ separateGraphCircles }} = require({json.dumps(module_path)});
      function assertSeparated(circles, gap) {{
        for (let left = 0; left < circles.length; left++) {{
          for (let right = left + 1; right < circles.length; right++) {{
            const a = circles[left];
            const b = circles[right];
            const distance = Math.hypot(a.x - b.x, a.y - b.y);
            if (distance + 0.001 < a.radius + b.radius + gap) process.exit(1);
          }}
        }}
      }}

      const chain = [
        {{ id: "root", x: 0, y: 0, radius: 50 }},
        {{ id: "a", x: 20, y: 0, radius: 30 }},
        {{ id: "b", x: 40, y: 0, radius: 30 }},
      ];
      const separated = separateGraphCircles(chain, {{ anchorId: "root", gap: 10 }});
      const root = separated.circles.find((circle) => circle.id === "root");
      if (root.x !== 0 || root.y !== 0) process.exit(2);
      assertSeparated(separated.circles, 10);

      const coincident = [
        {{ id: "root", x: 10, y: 10, radius: 20 }},
        {{ id: "same", x: 10, y: 10, radius: 20 }},
      ];
      const first = separateGraphCircles(coincident, {{ anchorId: "root", gap: 8 }});
      const second = separateGraphCircles(coincident, {{ anchorId: "root", gap: 8 }});
      assertSeparated(first.circles, 8);
      if (JSON.stringify(first.circles) !== JSON.stringify(second.circles)) process.exit(3);
    """

    subprocess.run([NODE, "-e", script], check=True, cwd=Path.cwd())


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_graph_avatar_scale_defaults_clamps_and_keeps_root_largest() -> None:
    app_path = str((STATIC_DIR / "app.js").resolve())
    script = f"""
      const fs = require("fs");
      const vm = require("vm");
      class Coordinator {{}}
      const context = {{
        console,
        setTimeout,
        clearTimeout,
        URLSearchParams,
        localStorage: {{ getItem() {{ return null; }}, setItem() {{}}, removeItem() {{}} }},
        window: {{
          LatestRequestCoordinator: Coordinator,
          GraphLifecycleCoordinator: Coordinator,
          addEventListener() {{}},
        }},
        document: {{ addEventListener() {{}}, getElementById() {{ return null; }} }},
      }};
      context.globalThis = context;
      vm.createContext(context);
      vm.runInContext(fs.readFileSync({json.dumps(app_path)}, "utf8"), context);

      const values = vm.runInContext(`({{
        minimum: clampGraphAvatarScale(1),
        maximum: clampGraphAvatarScale(999),
        fallback: clampGraphAvatarScale("invalid"),
        root75: graphNodeDiameter(1, true, 75),
        root150: graphNodeDiameter(1, true, 150),
        root225: graphNodeDiameter(1, true, 225),
        nonRoot150: graphNodeDiameter(100, false, 150),
      }})`, context);
      if (values.minimum !== 75 || values.maximum !== 225 || values.fallback !== 150) process.exit(1);
      if (values.root75 !== 69 || values.root150 !== 138 || values.root225 !== 207) process.exit(2);
      if (values.nonRoot150 >= values.root150) process.exit(3);
    """

    subprocess.run([NODE, "-e", script], check=True, cwd=Path.cwd())


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_graph_root_input_uses_resolved_recent_vanity_ids_without_network_access() -> None:
    app_path = str((STATIC_DIR / "app.js").resolve())
    recent_roots = json.dumps(
        [
            {
                "url": "https://steamcommunity.com/id/known-user/",
                "id": "76561198000000001",
            }
        ]
    )
    script = f"""
      const fs = require("fs");
      const vm = require("vm");
      class Coordinator {{}}
      let fetchCalls = 0;
      const context = {{
        console,
        setTimeout,
        clearTimeout,
        URLSearchParams,
        fetch() {{ fetchCalls++; throw new Error("network must not be used"); }},
        localStorage: {{
          getItem(key) {{ return key === "sfm_recent_roots" ? {json.dumps(recent_roots)} : null; }},
          setItem() {{}},
          removeItem() {{}},
        }},
        window: {{
          LatestRequestCoordinator: Coordinator,
          GraphLifecycleCoordinator: Coordinator,
          addEventListener() {{}},
        }},
        document: {{ addEventListener() {{}}, getElementById() {{ return null; }} }},
      }};
      context.globalThis = context;
      vm.createContext(context);
      vm.runInContext(fs.readFileSync({json.dumps(app_path)}, "utf8"), context);

      const numeric = vm.runInContext(
        'normalizeGraphRootInput("https://steamcommunity.com/profiles/76561198000000000/")',
        context,
      );
      const known = vm.runInContext(
        'normalizeGraphRootInput("https://steamcommunity.com/id/known-user/")',
        context,
      );
      const unknown = vm.runInContext(
        'normalizeGraphRootInput("https://steamcommunity.com/id/unknown-user/")',
        context,
      );
      if (numeric.value !== "76561198000000000") process.exit(1);
      if (known.value !== "76561198000000001" || known.unknownVanity) process.exit(2);
      if (unknown.value !== "" || unknown.unknownVanity !== "unknown-user") process.exit(3);
      if (fetchCalls !== 0) process.exit(4);
    """

    subprocess.run([NODE, "-e", script], check=True, cwd=Path.cwd())


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_latest_request_coordinator_aborts_and_invalidates_older_requests() -> None:
    module_path = str((STATIC_DIR / "request-coordinator.js").resolve())
    script = f"""
      const {{ LatestRequestCoordinator }} = require({json.dumps(module_path)});
      const coordinator = new LatestRequestCoordinator();
      const first = coordinator.begin("graph");
      if (!first.isCurrent() || first.signal.aborted) process.exit(1);

      const second = coordinator.begin("graph");
      if (!first.signal.aborted || first.isCurrent() || !second.isCurrent()) process.exit(2);
      first.finish();
      if (!second.isCurrent()) process.exit(3);

      const stats = coordinator.begin("db-stats");
      coordinator.cancelMany(["graph", "db-stats"]);
      if (!second.signal.aborted || !stats.signal.aborted) process.exit(4);
      if (second.isCurrent() || stats.isCurrent()) process.exit(5);
    """

    subprocess.run([NODE, "-e", script], check=True, cwd=Path.cwd())


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_graph_lifecycle_cancels_stale_chunks_and_layouts() -> None:
    module_path = str((STATIC_DIR / "graph-lifecycle.js").resolve())
    script = f"""
      const {{ GraphLifecycleCoordinator }} = require({json.dumps(module_path)});
      let nextTimer = 0;
      const callbacks = new Map();
      const cleared = new Set();
      const coordinator = new GraphLifecycleCoordinator({{
        setTimeout(callback) {{
          const id = ++nextTimer;
          callbacks.set(id, callback);
          return id;
        }},
        clearTimeout(id) {{ cleared.add(id); }},
      }});

      const firstRender = coordinator.beginRender();
      let staleCalls = 0;
      if (!coordinator.scheduleChunk(firstRender, () => staleCalls++, 10)) process.exit(1);
      const staleTimer = nextTimer;
      const secondRender = coordinator.beginRender();
      if (!cleared.has(staleTimer) || coordinator.isCurrent(firstRender)) process.exit(2);
      callbacks.get(staleTimer)();
      if (staleCalls !== 0) process.exit(3);

      let currentCalls = 0;
      if (!coordinator.scheduleChunk(secondRender, () => currentCalls++, 10)) process.exit(4);
      callbacks.get(nextTimer)();
      if (currentCalls !== 1) process.exit(5);

      function fakeLayout() {{
        return {{
          runs: 0,
          stops: 0,
          one(_event, callback) {{ this.onStop = callback; }},
          run() {{ this.runs++; }},
          stop() {{ this.stops++; if (this.onStop) this.onStop(); }},
        }};
      }}
      const firstLayout = fakeLayout();
      const secondLayout = fakeLayout();
      coordinator.startLayout(firstLayout);
      coordinator.startLayout(secondLayout);
      if (firstLayout.runs !== 1 || firstLayout.stops !== 1 || secondLayout.runs !== 1) process.exit(6);
      coordinator.cancel();
      if (secondLayout.stops !== 1 || coordinator.isCurrent(secondRender)) process.exit(7);
    """

    subprocess.run([NODE, "-e", script], check=True, cwd=Path.cwd())


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_frontend_javascript_parses() -> None:
    for filename in (
        "request-coordinator.js",
        "graph-lifecycle.js",
        "graph-collision.js",
        "app.js",
    ):
        subprocess.run(
            [NODE, "--check", str(STATIC_DIR / filename)],
            check=True,
            cwd=Path.cwd(),
        )


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_frontend_rejects_unsafe_profile_and_avatar_urls() -> None:
    app_path = str((STATIC_DIR / "app.js").resolve())
    script = f"""
      const fs = require("fs");
      const vm = require("vm");
      const context = {{
        console: {{ error() {{}}, log() {{}}, warn() {{}} }},
        setTimeout,
        clearTimeout,
        URL,
        URLSearchParams,
        localStorage: {{ getItem() {{ return null; }}, setItem() {{}} }},
        window: {{
          addEventListener() {{}},
          location: {{ origin: "http://localhost:8000" }},
        }},
        document: {{
          addEventListener() {{}},
          getElementById() {{ return null; }},
        }},
      }};
      context.globalThis = context;
      vm.createContext(context);
      vm.runInContext(fs.readFileSync({json.dumps(app_path)}, "utf8"), context);
      const evaluate = (expression) => vm.runInContext(expression, context);
      if (evaluate('safeExternalUrl("javascript:alert(1)")') !== "") process.exit(1);
      if (evaluate('safeExternalUrl("http://evil.example/avatar.png", {{ image: true }})') !== "") process.exit(2);
      if (evaluate('safeExternalUrl("data:text/html,boom", {{ image: true }})') !== "") process.exit(3);
      if (!evaluate('safeExternalUrl("https://steamcommunity.com/id/example")').startsWith("https://")) process.exit(4);
      if (!evaluate('safeExternalUrl("/static/avatar.png", {{ image: true }})').startsWith("http://localhost:8000/")) process.exit(5);
    """

    subprocess.run([NODE, "-e", script], check=True, cwd=Path.cwd())

    source = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    recent_roots = source.split("function renderRecentRoots()", 1)[1].split("const PRESET_KEY", 1)[
        0
    ]
    assert "onerror=" not in recent_roots


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_export_uses_native_download_without_buffering_blob() -> None:
    app_path = str((STATIC_DIR / "app.js").resolve())
    script = f"""
      const fs = require("fs");
      const vm = require("vm");
      const forms = [];
      const messages = [];
      const context = {{
        console: {{ error() {{}}, log() {{}}, warn() {{}} }},
        setTimeout,
        clearTimeout,
        URLSearchParams,
        fetch() {{ throw new Error("export must not use fetch"); }},
        localStorage: {{ getItem() {{ return null; }}, setItem() {{}} }},
        window: {{ addEventListener() {{}} }},
        document: {{
          addEventListener() {{}},
          getElementById() {{ return null; }},
          createElement(tag) {{
            if (tag !== "form") throw new Error(`unexpected element: ${{tag}}`);
            const form = {{
              submitted: false,
              removed: false,
              submit() {{ this.submitted = true; }},
              remove() {{ this.removed = true; }},
            }};
            forms.push(form);
            return form;
          }},
          body: {{ appendChild() {{}} }},
        }},
        messages,
      }};
      context.globalThis = context;
      vm.createContext(context);
      vm.runInContext(fs.readFileSync({json.dumps(app_path)}, "utf8"), context);
      vm.runInContext(`
        toast = (message) => messages.push(message);
        t = (key, values = {{}}) => values.message ? key + ":" + values.message : key;
        exportFile("csv");
      `, context);

      if (forms.length !== 1) process.exit(1);
      const form = forms[0];
      if (form.method !== "POST" || form.action !== "/api/export?format=csv") process.exit(2);
      if (form.target !== "exportFrame" || !form.hidden) process.exit(3);
      if (!form.submitted || !form.removed) process.exit(4);
      if (messages.at(-1) !== "toast.exportCsv") process.exit(5);

      context.testFrame = {{
        contentWindow: {{ location: {{ href: "http://localhost/api/export" }} }},
        contentDocument: {{ body: {{ textContent: '{{"detail":"download failed"}}' }} }},
      }};
      vm.runInContext("handleExportFrameLoad({{ currentTarget: testFrame }})", context);
      if (messages.at(-1) !== "toast.exportFailed:download failed") process.exit(6);
    """

    subprocess.run([NODE, "-e", script], check=True, cwd=Path.cwd())


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_settings_save_uses_one_atomic_request() -> None:
    app_path = str((STATIC_DIR / "app.js").resolve())
    script = f"""
      const fs = require("fs");
      const vm = require("vm");
      (async () => {{
      const requests = [];
      function field(value = "") {{
        const error = {{ textContent: "" }};
        return {{
          value,
          classList: {{ toggle() {{}} }},
          parentElement: {{
            querySelector() {{ return error; }},
            appendChild() {{}},
          }},
        }};
      }}
      const elements = {{
        settingsGraphDbEngine: field("kuzu"),
        settingsKuzuDbPath: field("data/graph_kuzu"),
        settingsKuzuBufferPoolSizeGb: field("2"),
        settingsNeo4jUri: field("bolt://localhost:7687"),
        settingsNeo4jUser: field("neo4j"),
        steamApiKeyInput: field("steam-secret"),
        steamProxyInput: field("socks5://127.0.0.1:1080"),
        neo4jPasswordInput: field("neo4j-secret"),
      }};
      const context = {{
        console: {{ error() {{}}, log() {{}}, warn() {{}} }},
        requests,
        setTimeout,
        clearTimeout,
        URL,
        URLSearchParams,
        localStorage: {{ getItem() {{ return null; }}, setItem() {{}} }},
        window: {{ addEventListener() {{}} }},
        document: {{
          addEventListener() {{}},
          getElementById(id) {{ return elements[id] || null; }},
          createElement() {{ return {{ className: "", textContent: "" }}; }},
        }},
      }};
      context.globalThis = context;
      vm.createContext(context);
      vm.runInContext(fs.readFileSync({json.dumps(app_path)}, "utf8"), context);
      await vm.runInContext(`
        api = async (path, options) => {{ requests.push({{ path, options }}); return {{}}; }};
        loadSettings = async () => {{}};
        testSettings = async () => {{}};
        toast = () => {{}};
        t = (key) => key;
        saveSettings();
      `, context);

      if (requests.length !== 1) process.exit(1);
      if (requests[0].path !== "/api/settings" || requests[0].options.method !== "PUT") process.exit(2);
      const payload = JSON.parse(requests[0].options.body);
      if (payload.graph_db_engine !== "kuzu" || payload.kuzu_buffer_pool_size_gb !== 2) process.exit(3);
      if (payload.steam_api_key !== "steam-secret") process.exit(4);
      if (payload.steam_proxy_url !== "socks5://127.0.0.1:1080") process.exit(5);
      if (payload.neo4j_password !== "neo4j-secret") process.exit(6);
      if (elements.steamApiKeyInput.value || elements.steamProxyInput.value || elements.neo4jPasswordInput.value) process.exit(7);
      }})().catch((error) => {{ console.error(error); process.exit(8); }});
    """

    subprocess.run([NODE, "-e", script], check=True, cwd=Path.cwd())


def test_request_coordinator_loads_before_application_script() -> None:
    index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    assert index.index("/static/request-coordinator.js") < index.index("/static/app.js")
    assert index.index("/static/graph-lifecycle.js") < index.index("/static/app.js")
    assert index.index("/static/graph-collision.js") < index.index("/static/app.js")
    script_sources = re.findall(r'<script src="([^"]+)"', index)
    assert script_sources
    assert all("?v=" in source for source in script_sources)


def test_required_interactive_elements_exist_in_application_page() -> None:
    source = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    declaration = source.split("const REQUIRED_INTERACTIVE_ELEMENT_IDS = [", 1)[1].split("];", 1)[0]
    required_ids = set(re.findall(r'"([A-Za-z0-9_-]+)"', declaration))
    page_ids = set(re.findall(r'id="([A-Za-z0-9_-]+)"', index))

    assert required_ids
    assert required_ids <= page_ids


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_application_uses_memory_fallback_when_local_storage_is_blocked() -> None:
    app_path = str((STATIC_DIR / "app.js").resolve())
    script = f"""
      const fs = require("fs");
      const vm = require("vm");
      const domReadyCallbacks = [];
      const context = {{
        console: {{ error() {{}}, log() {{}}, warn() {{}} }},
        setTimeout,
        clearTimeout,
        window: {{ addEventListener() {{}} }},
        document: {{
          documentElement: {{
            removeAttribute() {{}},
            toggleAttribute() {{}},
          }},
          addEventListener(event, callback) {{
            if (event === "DOMContentLoaded") domReadyCallbacks.push(callback);
          }},
          getElementById() {{ return null; }},
        }},
      }};
      Object.defineProperty(context, "localStorage", {{
        get() {{ throw new DOMException("storage blocked", "SecurityError"); }},
      }});
      context.globalThis = context;
      vm.createContext(context);
      vm.runInContext(fs.readFileSync({json.dumps(app_path)}, "utf8"), context);
      vm.runInContext('appStorage.setItem("session-key", "value")', context);
      if (vm.runInContext('appStorage.getItem("session-key")', context) !== "value") process.exit(1);
      vm.runInContext('appStorage.removeItem("session-key")', context);
      if (vm.runInContext('appStorage.getItem("session-key")', context) !== null) process.exit(2);
      if (domReadyCallbacks.length !== 1) process.exit(3);
      vm.runInContext('initTheme()', context);
      const overlayWorks = vm.runInContext(`
        (() => {{
          const fallback = createSafeStorage(() => ({{
            getItem() {{ return "stale"; }},
            setItem() {{ throw new DOMException("quota", "QuotaExceededError"); }},
            removeItem() {{ throw new DOMException("blocked", "SecurityError"); }},
          }}));
          fallback.setItem("key", "fresh");
          if (fallback.getItem("key") !== "fresh") return false;
          fallback.removeItem("key");
          return fallback.getItem("key") === null;
        }})()
      `, context);
      if (!overlayWorks) process.exit(4);
    """

    subprocess.run([NODE, "-e", script], check=True, cwd=Path.cwd())


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_application_reports_missing_helper_scripts_without_crashing() -> None:
    app_path = str((STATIC_DIR / "app.js").resolve())
    script = f"""
      const fs = require("fs");
      const vm = require("vm");
      const domReadyCallbacks = [];
      let legacyThemeListener = null;
      const title = {{ textContent: "" }};
      const hint = {{ textContent: "" }};
      const buttons = [{{ disabled: false }}, {{ disabled: false }}];
      const classList = () => ({{
        add() {{}},
        remove() {{}},
        toggle() {{}},
        contains() {{ return false; }},
      }});
      const elements = {{
        graphEmpty: {{
          classList: classList(),
          querySelector(selector) {{ return selector === "h3" ? title : hint; }},
        }},
        graphLoading: {{ classList: classList() }},
        graph: {{
          closest() {{ return {{ querySelectorAll() {{ return buttons; }} }}; }},
        }},
      }};
      const context = {{
        console: {{ error() {{}}, log() {{}}, warn() {{}} }},
        setTimeout,
        clearTimeout,
        fetch: async () => {{ throw new Error("missing i18n"); }},
        localStorage: {{ getItem() {{ return null; }}, setItem() {{}} }},
        window: {{
          addEventListener() {{}},
          matchMedia() {{
            return {{
              matches: false,
              addListener(callback) {{ legacyThemeListener = callback; }},
            }};
          }},
        }},
        document: {{
          documentElement: {{
            lang: "",
            setAttribute() {{}},
            removeAttribute() {{}},
            toggleAttribute() {{}},
          }},
          title: "",
          addEventListener(event, callback) {{
            if (event === "DOMContentLoaded") domReadyCallbacks.push(callback);
          }},
          getElementById(id) {{ return elements[id] || null; }},
          querySelectorAll() {{ return []; }},
        }},
      }};
      context.globalThis = context;
      vm.runInNewContext(fs.readFileSync({json.dumps(app_path)}, "utf8"), context);
      if (domReadyCallbacks.length !== 1) process.exit(1);
      domReadyCallbacks[0]().then(() => {{
        if (title.textContent !== "界面启动失败") process.exit(2);
        if (!hint.textContent.includes("request-coordinator.js")) process.exit(3);
        if (!hint.textContent.includes("graph-lifecycle.js")) process.exit(4);
        if (!hint.textContent.includes("vendor/cytoscape.min.js")) process.exit(5);
        if (buttons.some((button) => !button.disabled)) process.exit(6);
        if (!hint.textContent.includes("#graphRoot")) process.exit(7);
        if (typeof legacyThemeListener !== "function") process.exit(8);
        if (!hint.textContent.includes("graph-collision.js")) process.exit(9);
      }}).catch(() => process.exit(10));
    """

    subprocess.run([NODE, "-e", script], check=True, cwd=Path.cwd())


def test_application_reports_missing_frontend_dependencies_before_initializing_graph() -> None:
    source = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    startup = source.index('document.addEventListener("DOMContentLoaded"')
    dependency_check = source.index("getMissingFrontendDependencies()", startup)
    failure_display = source.index("showStartupFailure(missingDependencies)", dependency_check)
    graph_init = source.index("initGraph()", failure_display)

    assert dependency_check < failure_display < graph_init
    assert "button.disabled = true" in source
    db_poll_start = source.index("function scheduleDbStatsPoll()")
    db_poll_end = source.index("function startDbStatsPolling()", db_poll_start)
    assert "setInterval" not in source[db_poll_start:db_poll_end]
    startup_end = source.index("// ── Apple Ecosystem", startup)
    startup_source = source[startup:startup_end]
    assert "loadHealth()" in startup_source
    assert "testSettings(" not in startup_source

    terminal_start = source.index("if (TERMINAL_CRAWL_STATUSES.has(run.status))")
    terminal_end = source.index("scheduleRunPoll();", terminal_start)
    terminal_source = source[terminal_start:terminal_end]
    assert terminal_source.index(
        '$("graphRoot").value = run.root_steam_id'
    ) < terminal_source.index("await loadGraph()")


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_background_polling_retries_without_overlap_and_stops_on_pagehide() -> None:
    app_path = str((STATIC_DIR / "app.js").resolve())
    coordinator_path = str((STATIC_DIR / "request-coordinator.js").resolve())
    script = f"""
      const fs = require("fs");
      const vm = require("vm");
      const {{ LatestRequestCoordinator }} = require({json.dumps(coordinator_path)});
      (async () => {{
      let nextTimer = 0;
      const timers = new Map();
      const delays = [];
      const windowEvents = new Map();
      let fetchImpl;
      const systemLogLevel = {{ value: "" }};
      const context = {{
        console: {{ error() {{}}, log() {{}}, warn() {{}} }},
        setTimeout(callback, delay) {{
          const id = ++nextTimer;
          timers.set(id, callback);
          delays.push(delay);
          return id;
        }},
        clearTimeout(id) {{ timers.delete(id); }},
        setInterval() {{ return 999; }},
        clearInterval() {{}},
        URLSearchParams,
        fetch(...args) {{ return fetchImpl(...args); }},
        localStorage: {{ getItem() {{ return null; }}, setItem() {{}} }},
        window: {{
          LatestRequestCoordinator,
          addEventListener(event, callback) {{ windowEvents.set(event, callback); }},
        }},
        document: {{
          hidden: false,
          addEventListener() {{}},
          getElementById(id) {{ return id === "systemLogLevel" ? systemLogLevel : null; }},
        }},
      }};
      context.globalThis = context;
      vm.createContext(context);
      vm.runInContext(fs.readFileSync({json.dumps(app_path)}, "utf8"), context);

      fetchImpl = async () => ({{
        ok: false,
        statusText: "temporary failure",
        json: async () => ({{ detail: "temporary failure" }}),
      }});
      await vm.runInContext('currentRunId = "run-1"; pollRun()', context);
      if (delays.at(-1) !== 2400 || timers.size !== 1) process.exit(1);

      timers.clear();
      delays.length = 0;
      let resolveFetch;
      fetchImpl = () => new Promise((resolve) => {{ resolveFetch = resolve; }});
      vm.runInContext("startSystemLogPolling()", context);
      const [firstTimerId, firstTimer] = [...timers.entries()][0];
      timers.delete(firstTimerId);
      const pendingPoll = firstTimer();
      if (timers.size !== 0) process.exit(2);
      resolveFetch({{ ok: true, json: async () => [] }});
      await pendingPoll;
      if (timers.size !== 1 || delays.at(-1) !== 2500) process.exit(3);

      windowEvents.get("pagehide")();
      if (timers.size !== 0) process.exit(4);
      }})().catch((error) => {{ console.error(error); process.exit(5); }});
    """

    subprocess.run([NODE, "-e", script], check=True, cwd=Path.cwd())


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_force_stop_keeps_polling_until_the_server_reports_a_terminal_state() -> None:
    app_path = str((STATIC_DIR / "app.js").resolve())
    coordinator_path = str((STATIC_DIR / "request-coordinator.js").resolve())
    script = f"""
      const fs = require("fs");
      const vm = require("vm");
      const {{ LatestRequestCoordinator }} = require({json.dumps(coordinator_path)});
      (async () => {{
      const requests = [];
      const pollDelays = [];
      const timerStops = [];
      const statsStops = [];
      const context = {{
        console: {{ error() {{}}, log() {{}}, warn() {{}} }},
        setTimeout,
        clearTimeout,
        URLSearchParams,
        localStorage: {{ getItem() {{ return null; }}, setItem() {{}} }},
        window: {{ LatestRequestCoordinator, addEventListener() {{}} }},
        document: {{ addEventListener() {{}}, getElementById() {{ return null; }} }},
        requests,
        pollDelays,
        timerStops,
        statsStops,
      }};
      context.globalThis = context;
      vm.createContext(context);
      vm.runInContext(fs.readFileSync({json.dumps(app_path)}, "utf8"), context);
      await vm.runInContext(`
        currentRunId = "run-1";
        api = async (path, options) => {{ requests.push({{ path, options }}); return {{ stopped: true }}; }};
        scheduleRunPoll = (delay) => pollDelays.push(delay);
        stopTimer = () => timerStops.push(true);
        stopDbStatsPolling = () => statsStops.push(true);
        toast = () => {{}};
        t = (key) => key;
        forceStopCrawl();
      `, context);

      if (requests.length !== 1) process.exit(1);
      if (requests[0].path !== "/api/crawls/run-1/force-stop") process.exit(2);
      if (requests[0].options.method !== "POST") process.exit(3);
      if (pollDelays.length !== 1 || pollDelays[0] !== 100) process.exit(4);
      if (timerStops.length !== 0 || statsStops.length !== 0) process.exit(5);
      }})().catch((error) => {{ console.error(error); process.exit(6); }});
    """

    subprocess.run([NODE, "-e", script], check=True, cwd=Path.cwd())


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_frontend_reattaches_to_an_active_crawl_after_reload() -> None:
    app_path = str((STATIC_DIR / "app.js").resolve())
    coordinator_path = str((STATIC_DIR / "request-coordinator.js").resolve())
    script = f"""
      const fs = require("fs");
      const vm = require("vm");
      const {{ LatestRequestCoordinator }} = require({json.dumps(coordinator_path)});
      (async () => {{
      const requests = [];
      const timerStarts = [];
      const logs = [];
      const calls = {{ stats: 0, poll: 0 }};
      const elements = {{
        crawlLogs: {{ innerHTML: "old" }},
        graphRoot: {{ value: "" }},
        analysisRoot: {{ value: "" }},
        crawlStatus: {{ dataset: {{}}, textContent: "" }},
        nodeCount: {{ textContent: "" }},
        edgeCount: {{ textContent: "" }},
        privateCount: {{ textContent: "" }},
        filteredCount: {{ textContent: "" }},
        lastEvent: {{ textContent: "" }},
        crawlProgressBar: {{ style: {{}} }},
        cancelCrawl: {{ style: {{}} }},
        forceStopCrawl: {{ style: {{}} }},
        pauseCrawl: {{ style: {{}} }},
        resumeCrawl: {{ style: {{}} }},
      }};
      const activeRun = {{
        id: "run-active",
        root_steam_id: "root",
        status: "pending",
        started_at: "2026-07-01T00:00:00Z",
        nodes_discovered: 3,
        edges_discovered: 2,
        private_count: 1,
        filtered_count: 4,
        progress_percent: 25,
        last_event: "queued",
      }};
      const context = {{
        console: {{ error() {{}}, log() {{}}, warn() {{}} }},
        setTimeout,
        clearTimeout,
        URLSearchParams,
        localStorage: {{ getItem() {{ return null; }}, setItem() {{}} }},
        window: {{ LatestRequestCoordinator, addEventListener() {{}} }},
        document: {{
          addEventListener() {{}},
          getElementById(id) {{ return elements[id] || null; }},
        }},
        requests,
        timerStarts,
        logs,
        calls,
        activeRun,
      }};
      context.globalThis = context;
      vm.createContext(context);
      vm.runInContext(fs.readFileSync({json.dumps(app_path)}, "utf8"), context);
      const recovered = await vm.runInContext(`
        latestApi = async (key, path) => {{ requests.push({{ key, path }}); return activeRun; }};
        startTimer = (startedAt) => timerStarts.push(startedAt);
        startDbStatsPolling = () => calls.stats++;
        appendSystemLog = (_level, _source, message) => logs.push(message);
        pollRun = async () => {{ calls.poll++; }};
        t = (key) => key;
        recoverActiveCrawl();
      `, context);

      if (!recovered || requests.length !== 1) process.exit(1);
      if (requests[0].path !== "/api/crawls/active") process.exit(2);
      if (vm.runInContext("currentRunId", context) !== "run-active") process.exit(3);
      if (elements.graphRoot.value !== "root" || elements.analysisRoot.value !== "root") process.exit(4);
      if (elements.crawlStatus.dataset.status !== "pending") process.exit(5);
      if (elements.crawlProgressBar.style.width !== "25%") process.exit(6);
      if (elements.cancelCrawl.style.display !== "" || elements.forceStopCrawl.style.display !== "") process.exit(7);
      if (elements.pauseCrawl.style.display !== "none" || elements.resumeCrawl.style.display !== "none") process.exit(8);
      if (timerStarts[0] !== activeRun.started_at || calls.stats !== 1 || calls.poll !== 1) process.exit(9);
      if (logs[0] !== "log.crawlReattached" || elements.crawlLogs.innerHTML !== "") process.exit(10);
      }})().catch((error) => {{ console.error(error); process.exit(11); }});
    """

    subprocess.run([NODE, "-e", script], check=True, cwd=Path.cwd())


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_stale_frontend_responses_cannot_overwrite_current_state() -> None:
    app_path = str((STATIC_DIR / "app.js").resolve())
    coordinator_path = str((STATIC_DIR / "request-coordinator.js").resolve())
    script = f"""
      const fs = require("fs");
      const vm = require("vm");
      const {{ LatestRequestCoordinator }} = require({json.dumps(coordinator_path)});
      (async () => {{
      const pending = [];
      const systemMessages = [];
      const crawlMessages = [];
      const classList = () => ({{ toggle() {{}}, contains() {{ return false; }} }});
      const elements = {{
        steamStatus: {{ dataset: {{}}, textContent: "" }},
        neo4jStatus: {{ dataset: {{}}, textContent: "" }},
        steamStatusDetail: {{ dataset: {{}}, textContent: "", classList: classList() }},
        neo4jStatusDetail: {{ dataset: {{}}, textContent: "", classList: classList() }},
        systemLogs: {{ innerHTML: "" }},
        systemLogLevel: {{ value: "" }},
      }};
      const context = {{
        console: {{ error() {{}}, log() {{}}, warn() {{}} }},
        pending,
        systemMessages,
        crawlMessages,
        setTimeout,
        clearTimeout,
        URLSearchParams,
        fetch(path, options = {{}}) {{
          return new Promise((resolve) => pending.push({{ path, options, resolve }}));
        }},
        localStorage: {{ getItem() {{ return null; }}, setItem() {{}} }},
        window: {{ LatestRequestCoordinator, addEventListener() {{}} }},
        document: {{
          hidden: false,
          addEventListener() {{}},
          getElementById(id) {{ return elements[id] || null; }},
        }},
      }};
      context.globalThis = context;
      vm.createContext(context);
      vm.runInContext(fs.readFileSync({json.dumps(app_path)}, "utf8"), context);
      vm.runInContext(`
        t = (key) => key;
        toast = () => {{}};
        loadDbStats = async () => {{}};
        appendSystemLog = (_level, _source, message) => systemMessages.push(message);
        appendUiLog = (_level, _stage, message) => crawlMessages.push(message);
      `, context);

      function request(path) {{
        const item = pending.find((entry) => entry.path === path && !entry.resolved);
        if (!item) throw new Error(`missing request: ${{path}}`);
        return item;
      }}
      function resolve(item, body) {{
        item.resolved = true;
        item.resolve({{ ok: true, json: async () => body }});
      }}

      const healthPromise = vm.runInContext("loadHealth()", context);
      const healthRequest = request("/api/health");
      const testPromise = vm.runInContext("testSettings({{ silent: true }})", context);
      const testRequest = request("/api/settings/test");
      if (!healthRequest.options.signal.aborted) process.exit(1);
      resolve(testRequest, {{
        steam_ok: true,
        neo4j_ok: false,
        steam_message: "fresh Steam result",
        neo4j_message: "fresh database result",
      }});
      await testPromise;
      resolve(healthRequest, {{ database_message: "stale health result" }});
      await healthPromise;
      if (elements.neo4jStatusDetail.textContent !== "fresh database result") process.exit(2);

      systemMessages.length = 0;
      vm.runInContext("lastSystemLogSeq = 5", context);
      const staleLogsPromise = vm.runInContext("loadSystemLogs()", context);
      const staleLogsRequest = request("/api/logs?after=5");
      const freshLogsPromise = vm.runInContext("loadSystemLogs(true)", context);
      const freshLogsRequest = request("/api/logs?after=0");
      if (!staleLogsRequest.options.signal.aborted) process.exit(3);
      resolve(freshLogsRequest, [{{ seq: 1, level: "info", source: "new", message: "fresh log" }}]);
      await freshLogsPromise;
      resolve(staleLogsRequest, [{{ seq: 6, level: "warn", source: "old", message: "stale log" }}]);
      await staleLogsPromise;
      if (systemMessages.join(",") !== "fresh log") process.exit(4);
      if (vm.runInContext("lastSystemLogSeq", context) !== 1) process.exit(5);

      vm.runInContext('currentRunId = "run-old"; lastEventSeq = 0; pageActive = true', context);
      const staleEventsPromise = vm.runInContext("loadEvents()", context);
      const staleEventsRequest = request("/api/crawls/run-old/events?after=0");
      vm.runInContext('currentRunId = "run-new"', context);
      const freshEventsPromise = vm.runInContext("loadEvents()", context);
      const freshEventsRequest = request("/api/crawls/run-new/events?after=0");
      if (!staleEventsRequest.options.signal.aborted) process.exit(6);
      resolve(freshEventsRequest, [{{ seq: 1, level: "info", stage: "new", message: "fresh event" }}]);
      await freshEventsPromise;
      resolve(staleEventsRequest, [{{ seq: 9, level: "warn", stage: "old", message: "stale event" }}]);
      await staleEventsPromise;
      if (crawlMessages.join(",") !== "fresh event") process.exit(7);
      if (vm.runInContext("lastEventSeq", context) !== 1) process.exit(8);
      }})().catch((error) => {{ console.error(error); process.exit(9); }});
    """

    subprocess.run([NODE, "-e", script], check=True, cwd=Path.cwd())
