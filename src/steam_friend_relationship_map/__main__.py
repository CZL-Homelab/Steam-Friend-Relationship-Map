from __future__ import annotations

import uvicorn

from .settings import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "steam_friend_relationship_map.app:create_app",
        host=settings.app_host,
        port=settings.app_port,
        factory=True,
        reload=False,
    )


if __name__ == "__main__":
    main()
