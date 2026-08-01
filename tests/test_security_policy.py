from __future__ import annotations

import re
from pathlib import Path

CREDENTIAL_URI = re.compile(
    r"\b(?:https?|socks5h?|bolt)://[^/\s:@]+:[^@\s/]+@",
    re.IGNORECASE,
)
TEXT_SUFFIXES = {".css", ".html", ".js", ".json", ".md", ".py", ".toml", ".yml", ".yaml"}


def test_current_tree_does_not_embed_credentials_in_uri_literals() -> None:
    roots = [Path("src"), Path("tests"), Path(".github")]
    files = [Path("README.md"), Path("SECURITY.md"), Path("pyproject.toml")]
    for root in roots:
        files.extend(
            path
            for path in root.rglob("*")
            if path.is_file()
            and path.suffix.lower() in TEXT_SUFFIXES
            and "vendor" not in path.parts
        )

    findings = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), start=1):
            if CREDENTIAL_URI.search(line):
                findings.append(f"{path}:{line_number}")

    assert findings == [], "Embedded credential URI literals: " + ", ".join(findings)
