from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


STATIC_DIR = Path("src/steam_friend_relationship_map/static")
NODE = shutil.which("node")


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
    for filename in ("request-coordinator.js", "graph-lifecycle.js", "app.js"):
        subprocess.run(
            [NODE, "--check", str(STATIC_DIR / filename)],
            check=True,
            cwd=Path.cwd(),
        )


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


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_application_reports_missing_helper_scripts_without_crashing() -> None:
    app_path = str((STATIC_DIR / "app.js").resolve())
    script = f"""
      const fs = require("fs");
      const vm = require("vm");
      const domReadyCallbacks = [];
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
          matchMedia() {{ return {{ matches: false, addEventListener() {{}} }}; }},
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
      }}).catch(() => process.exit(7));
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
