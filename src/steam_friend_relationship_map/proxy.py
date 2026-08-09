from __future__ import annotations

from urllib.parse import urlparse

SUPPORTED_PROXY_SCHEMES = frozenset({"http", "https", "socks5", "socks5h"})


def normalize_proxy_url(value: str) -> str:
    cleaned = value.replace("\r", "").replace("\n", "").strip()
    if not cleaned:
        return ""
    try:
        parsed = urlparse(cleaned)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("invalid proxy URL") from exc
    if parsed.scheme.lower() not in SUPPORTED_PROXY_SCHEMES or not hostname:
        schemes = ", ".join(sorted(SUPPORTED_PROXY_SCHEMES))
        raise ValueError(f"proxy URL must use one of: {schemes}")
    return cleaned
