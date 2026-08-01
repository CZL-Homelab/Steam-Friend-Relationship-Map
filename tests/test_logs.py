from __future__ import annotations

import gc
import logging
import weakref

from steam_friend_relationship_map.logs import (
    AppLogBuffer,
    AppLogHandler,
    install_log_handler,
    release_log_handler,
)


def test_install_log_handler_reuses_handler_and_targets_latest_buffer() -> None:
    first_buffer = AppLogBuffer()
    first_handler = install_log_handler(first_buffer)
    logger = logging.getLogger("steam_friend_relationship_map.lifecycle_test")
    second_buffer = AppLogBuffer()
    try:
        logger.info("first message")
        second_handler = install_log_handler(second_buffer)
        logger.info("second message")

        assert second_handler is first_handler
        assert [row.message for row in first_buffer.list()] == ["first message"]
        assert [row.message for row in second_buffer.list()] == ["second message"]

        release_log_handler(second_handler, second_buffer)
        logger.info("back to first")
        assert [row.message for row in first_buffer.list()] == [
            "first message",
            "back to first",
        ]
        assert [row.message for row in second_buffer.list()] == ["second message"]

        for logger_name in ("steam_friend_relationship_map", "uvicorn.error"):
            handlers = [
                handler
                for handler in logging.getLogger(logger_name).handlers
                if isinstance(handler, AppLogHandler)
            ]
            assert handlers == [first_handler]
    finally:
        release_log_handler(first_handler, second_buffer)
        release_log_handler(first_handler, first_buffer)


def test_log_handler_does_not_keep_an_unregistered_app_buffer_alive() -> None:
    buffer = AppLogBuffer()
    handler = AppLogHandler(buffer)
    buffer_reference = weakref.ref(buffer)

    del buffer
    gc.collect()

    assert buffer_reference() is None
    assert handler.buffer is None
