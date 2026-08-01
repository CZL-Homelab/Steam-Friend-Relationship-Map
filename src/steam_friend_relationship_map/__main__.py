from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

import uvicorn
from pydantic import ValidationError

from .settings import Settings, get_settings


def _atomic_write_text(path: Path, content: str) -> None:
    """Replace a text file only after its complete contents reach disk."""
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def init_env(force: bool = False) -> None:
    env_path = Path.cwd() / ".env"
    env_example_path = Path.cwd() / ".env.example"

    # If .env already exists and we are not forcing, do nothing
    if env_path.exists() and not force:
        return

    # Determine interactive status
    is_interactive = sys.stdin.isatty()

    default_port = 8000
    if env_example_path.exists():
        try:
            with open(env_example_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip().startswith("APP_PORT="):
                        port_str = line.split("=", 1)[1].strip()
                        if port_str.isdigit() and 1024 <= int(port_str) <= 65535:
                            default_port = int(port_str)
        except Exception:
            pass

    port = default_port
    if is_interactive:
        print("=" * 60)
        action_msg = "重新初始化" if env_path.exists() else "初始化"
        print(f"检测到正在进行环境配置，正在为您{action_msg} .env 配置文件...")
        print("=" * 60)

        while True:
            try:
                prompt_msg = f"请输入 Web UI 的本地端口 (APP_PORT) [默认: {default_port}]: "
                user_input = input(prompt_msg).strip()
                if not user_input:
                    port = default_port
                    break
                # 安全防范：清理任何换行符防止注入
                cleaned_input = user_input.replace("\r", "").replace("\n", "").strip()
                if cleaned_input.isdigit():
                    val = int(cleaned_input)
                    if 1024 <= val <= 65535:
                        port = val
                        break
                    else:
                        print("错误：端口范围必须在 1024 到 65535 之间。")
                else:
                    print("错误：请输入有效的整数端口号。")
            except (KeyboardInterrupt, EOFError):
                print("\n配置输入被中断，将使用默认端口配置。")
                port = default_port
                break
    else:
        # 非交互式环境下，静默使用默认端口
        pass

    # 读取模板
    template_content = ""
    if env_example_path.exists():
        try:
            template_content = env_example_path.read_text(encoding="utf-8")
        except Exception as e:
            print(f"警告：无法读取 .env.example 模板: {e}")

    if template_content:
        lines = []
        port_replaced = False
        for line in template_content.splitlines():
            if line.strip().startswith("APP_PORT="):
                lines.append(f"APP_PORT={port}")
                port_replaced = True
            else:
                lines.append(line)
        if not port_replaced:
            lines.append(f"APP_PORT={port}")
        new_content = "\n".join(lines) + "\n"
    else:
        new_content = (
            f"GRAPH_DB_ENGINE=kuzu\n"
            f"KUZU_DB_PATH=./data/graph_kuzu\n"
            f"KUZU_BUFFER_POOL_SIZE_GB=1\n"
            f"NEO4J_URI=bolt://localhost:7687\n"
            f"NEO4J_USER=neo4j\n"
            f"APP_HOST=127.0.0.1\n"
            f"APP_PORT={port}\n"
            f"DEFAULT_MAX_DEPTH=2\n"
            f"DEFAULT_MAX_NODES=2000\n"
            f"DEFAULT_DELAY_MS=300\n"
        )

    try:
        _atomic_write_text(env_path, new_content)
        print(f"配置成功：.env 文件已生成，Web UI 本地端口配置为 {port}。")
    except Exception as e:
        raise RuntimeError(f"无法安全写入 .env 配置文件: {e}") from e


def _print_settings_error(exc: ValidationError) -> None:
    print("启动配置无效，请检查 .env：", file=sys.stderr)
    for error in exc.errors(include_url=False):
        location_parts = list(error["loc"])
        if location_parts:
            field = Settings.model_fields.get(str(location_parts[0]))
            if field is not None and field.alias:
                location_parts[0] = field.alias
        location = ".".join(str(part) for part in location_parts)
        print(f"  - {location}: {error['msg']}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="Steam Friend Relationship Map")
    parser.add_argument("--init", action="store_true", help="Force re-initialize the .env configuration file")
    args = parser.parse_args()

    try:
        init_env(force=args.init)
    except Exception as exc:
        print(f"启动初始化失败：{exc}", file=sys.stderr)
        raise SystemExit(2) from None

    try:
        settings = get_settings()
    except ValidationError as exc:
        _print_settings_error(exc)
        raise SystemExit(2) from None
    except Exception as exc:
        print(f"启动配置读取失败：{exc}", file=sys.stderr)
        raise SystemExit(2) from None

    try:
        uvicorn.run(
            "steam_friend_relationship_map.app:create_app",
            host=settings.app_host,
            port=settings.app_port,
            factory=True,
            reload=False,
        )
    except Exception as exc:
        print(f"服务启动失败：{exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
