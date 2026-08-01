from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from steam_friend_relationship_map import __main__ as main_module
from steam_friend_relationship_map.__main__ import init_env
from steam_friend_relationship_map.settings import Settings


def test_settings_accepts_python_field_names() -> None:
    settings = Settings(
        app_host="0.0.0.0",
        app_port=8123,
        kuzu_db_path="data/custom",
        steam_proxy_url="http://127.0.0.1:8080",
    )

    assert settings.app_host == "0.0.0.0"
    assert settings.app_port == 8123
    assert settings.kuzu_db_path == "data/custom"
    assert settings.steam_proxy_url == "http://127.0.0.1:8080"


def test_settings_rejects_invalid_proxy_scheme() -> None:
    with pytest.raises(ValueError, match="proxy URL"):
        Settings(steam_proxy_url="ftp://127.0.0.1:21")


def test_get_settings_keeps_secrets_that_succeed_when_one_keyring_read_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from steam_friend_relationship_map import settings as settings_module
    from steam_friend_relationship_map.secrets import SecretStorageError

    class PartialSecretStore:
        def get(self, name: str) -> str:
            if name == "steam_proxy_url":
                raise SecretStorageError("proxy credential unavailable")
            return {
                "steam_api_key": "steam-key",
                "neo4j_password": "neo4j-password",
            }.get(name, "")

    monkeypatch.chdir(tmp_path)
    for env_key in ("STEAM_API_KEY", "STEAM_PROXY_URL", "NEO4J_PASSWORD"):
        monkeypatch.delenv(env_key, raising=False)
    monkeypatch.setattr(settings_module, "SecretStore", PartialSecretStore)
    settings_module.clear_settings_cache()
    try:
        loaded = settings_module.get_settings()
    finally:
        settings_module.clear_settings_cache()

    assert loaded.steam_api_key == "steam-key"
    assert loaded.steam_proxy_url == ""
    assert loaded.neo4j_password == "neo4j-password"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("graph_db_engine", "sqlite"),
        ("kuzu_db_path", ""),
        ("kuzu_buffer_pool_size_gb", 0),
        ("kuzu_buffer_pool_size_gb", 65),
        ("app_host", ""),
        ("app_port", 0),
        ("app_port", 65536),
        ("active_project", ""),
    ],
)
def test_settings_rejects_values_that_cannot_start_safely(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(**{field: value})


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


def test_init_env_interactive_invalid_then_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
        "GRAPH_DB_ENGINE=kuzu\nAPP_PORT=8000\nDEFAULT_MAX_DEPTH=2\n", encoding="utf-8"
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


def test_init_env_preserves_existing_file_when_atomic_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    env_file = tmp_path / ".env"
    env_file.write_text("APP_PORT=8123\nACTIVE_PROJECT=existing\n", encoding="utf-8")

    def fail_replace(_source: object, _target: object) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(main_module.os, "replace", fail_replace)

    with pytest.raises(RuntimeError, match="无法安全写入"):
        init_env(force=True)

    assert env_file.read_text(encoding="utf-8") == "APP_PORT=8123\nACTIVE_PROJECT=existing\n"
    assert list(tmp_path.glob("..env.*.tmp")) == []


def test_init_env_ignores_out_of_range_template_port(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    (tmp_path / ".env.example").write_text("APP_PORT=99999\n", encoding="utf-8")

    init_env()

    assert "APP_PORT=8000" in (tmp_path / ".env").read_text(encoding="utf-8")


def test_main_reports_invalid_settings_without_starting_server(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["steam-friend-map"])
    monkeypatch.setattr(main_module, "init_env", lambda force=False: None)
    with pytest.raises(ValidationError) as validation_error:
        Settings(app_port=70000)
    monkeypatch.setattr(
        main_module,
        "get_settings",
        MagicMock(side_effect=validation_error.value),
    )
    run = MagicMock()
    monkeypatch.setattr(main_module.uvicorn, "run", run)

    with pytest.raises(SystemExit) as exit_info:
        main_module.main()

    assert exit_info.value.code == 2
    stderr = capsys.readouterr().err
    assert "启动配置无效" in stderr
    assert "APP_PORT" in stderr
    run.assert_not_called()


def test_main_reports_settings_source_failure_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["steam-friend-map"])
    monkeypatch.setattr(main_module, "init_env", lambda force=False: None)
    monkeypatch.setattr(
        main_module,
        "get_settings",
        MagicMock(
            side_effect=UnicodeDecodeError(
                "utf-8",
                b"\xff",
                0,
                1,
                "invalid start byte",
            )
        ),
    )
    run = MagicMock()
    monkeypatch.setattr(main_module.uvicorn, "run", run)

    with pytest.raises(SystemExit) as exit_info:
        main_module.main()

    assert exit_info.value.code == 2
    stderr = capsys.readouterr().err
    assert "启动配置读取失败" in stderr
    assert "Traceback" not in stderr
    run.assert_not_called()


def test_main_reports_server_factory_failure_and_preserves_uvicorn_system_exit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["steam-friend-map"])
    monkeypatch.setattr(main_module, "init_env", lambda force=False: None)
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings())
    run = MagicMock(side_effect=RuntimeError("factory crashed"))
    monkeypatch.setattr(main_module.uvicorn, "run", run)

    with pytest.raises(SystemExit) as exit_info:
        main_module.main()

    assert exit_info.value.code == 1
    assert "服务启动失败：factory crashed" in capsys.readouterr().err

    run.side_effect = SystemExit(3)
    with pytest.raises(SystemExit) as uvicorn_exit:
        main_module.main()

    assert uvicorn_exit.value.code == 3
    assert capsys.readouterr().err == ""
