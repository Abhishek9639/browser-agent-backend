"""Shared FastAPI dependencies.

Settings are read from ``request.app.state``, not from the module-level cache,
so an app built with ``create_app(settings)`` really is configured by those
settings all the way down to the handlers. Tests depend on this.
"""

from typing import Annotated

from fastapi import Depends, Request

from app.core.config import Settings, get_settings

__all__ = ["SettingsDep", "get_app_settings"]


def get_app_settings(request: Request) -> Settings:
    settings = getattr(request.app.state, "settings", None)
    if isinstance(settings, Settings):
        return settings
    return get_settings()


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
