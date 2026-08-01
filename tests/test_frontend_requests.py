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


def test_request_coordinator_loads_before_application_script() -> None:
    index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    assert index.index("/static/request-coordinator.js") < index.index("/static/app.js")
    assert index.index("/static/graph-lifecycle.js") < index.index("/static/app.js")
