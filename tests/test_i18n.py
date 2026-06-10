from __future__ import annotations

import json
from pathlib import Path


I18N_PATH = Path("src/steam_friend_relationship_map/static/i18n.json")


def test_i18n_language_keys_match() -> None:
    data = json.loads(I18N_PATH.read_text(encoding="utf-8"))

    zh_keys = set(data["zh-CN"])
    en_keys = set(data["en"])

    assert zh_keys == en_keys


def test_i18n_required_keys_exist() -> None:
    data = json.loads(I18N_PATH.read_text(encoding="utf-8"))
    required = {
        "app.title",
        "action.startCrawl",
        "graph.summary",
        "graph.summaryLimited",
        "path.noPath",
        "status.running",
        "toast.rootRequired",
    }

    for lang in ("zh-CN", "en"):
        assert required <= set(data[lang])
