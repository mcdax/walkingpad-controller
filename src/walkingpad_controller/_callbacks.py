"""Callback fan-out shared by the controller and its protocol backends."""

from __future__ import annotations

import logging
from typing import Any, Callable


def fire_callbacks(
    callbacks: list[Callable[..., None]],
    logger: logging.Logger,
    error_message: str,
    *args: Any,
) -> None:
    """Call each callback in order; log and swallow its exceptions.

    Logged on the caller's ``logger`` so records keep their module's name.
    """
    for cb in callbacks:
        try:
            cb(*args)
        except Exception:
            logger.exception(error_message)
