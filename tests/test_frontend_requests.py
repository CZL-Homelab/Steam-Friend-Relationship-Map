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
def test_frontend_javascript_parses() -> None:
    for filename in ("request-coordinator.js", "app.js"):
        subprocess.run(
            [NODE, "--check", str(STATIC_DIR / filename)],
            check=True,
            cwd=Path.cwd(),
        )


def test_request_coordinator_loads_before_application_script() -> None:
    index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    assert index.index("/static/request-coordinator.js") < index.index("/static/app.js")
