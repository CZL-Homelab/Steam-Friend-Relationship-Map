from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from steam_friend_relationship_map.__main__ import init_env


def test_init_env_non_interactive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Set up cwd to be tmp_path
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)

    # Mock stdin.isatty to return False
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    # Call init_env
    init_env(force=False)

    # Verify that .env was created with default port
    env_file = tmp_path / ".env"
    assert env_file.exists()
    content = env_file.read_text(encoding="utf-8")
    assert "APP_PORT=8000" in content


def test_init_env_interactive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Set up cwd to be tmp_path
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)

    # Mock stdin.isatty to return True
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    # Mock input to return "8080"
    monkeypatch.setattr("builtins.input", lambda prompt="": "8080")

    # Call init_env
    init_env(force=True)

    # Verify that .env was created with custom port
    env_file = tmp_path / ".env"
    assert env_file.exists()
    content = env_file.read_text(encoding="utf-8")
    assert "APP_PORT=8080" in content


def test_init_env_interactive_invalid_then_valid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Set up cwd to be tmp_path
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)

    # Mock stdin.isatty to return True
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    # Mock input to return an invalid value first, then a valid value
    inputs = ["invalid_port", "90000", "8888"]
    input_mock = MagicMock(side_effect=inputs)
    monkeypatch.setattr("builtins.input", input_mock)

    # Call init_env
    init_env(force=True)

    # Verify that .env was created with valid port
    env_file = tmp_path / ".env"
    assert env_file.exists()
    content = env_file.read_text(encoding="utf-8")
    assert "APP_PORT=8888" in content
    assert input_mock.call_count == 3


def test_init_env_with_example_template(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Set up cwd to be tmp_path
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)

    # Create .env.example
    example_file = tmp_path / ".env.example"
    example_file.write_text(
        "GRAPH_DB_ENGINE=kuzu\nAPP_PORT=8000\nDEFAULT_MAX_DEPTH=2\n",
        encoding="utf-8"
    )

    # Mock stdin.isatty to return True
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "8088")

    # Call init_env
    init_env(force=True)

    # Verify that .env was created using example template
    env_file = tmp_path / ".env"
    assert env_file.exists()
    content = env_file.read_text(encoding="utf-8")
    assert "GRAPH_DB_ENGINE=kuzu" in content
    assert "APP_PORT=8088" in content
    assert "DEFAULT_MAX_DEPTH=2" in content
