from __future__ import annotations

import logging

from steam_friend_relationship_map.logs import AppLogBuffer, AppLogHandler, install_log_handler


def test_install_log_handler_reuses_handler_and_targets_latest_buffer() -> None:
    first_buffer = AppLogBuffer()
    first_handler = install_log_handler(first_buffer)
    logger = logging.getLogger("steam_friend_relationship_map.lifecycle_test")
    logger.info("first message")

    second_buffer = AppLogBuffer()
    second_handler = install_log_handler(second_buffer)
    logger.info("second message")

    assert second_handler is first_handler
    assert [row.message for row in first_buffer.list()] == ["first message"]
    assert [row.message for row in second_buffer.list()] == ["second message"]
    for logger_name in ("steam_friend_relationship_map", "uvicorn.error"):
        handlers = [
            handler
            for handler in logging.getLogger(logger_name).handlers
            if isinstance(handler, AppLogHandler)
        ]
        assert len(handlers) == 1
