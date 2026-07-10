"""Backward-compat shim. Real implementation lives in wren.providers.connection.

Kept so existing ``from wren_pydantic._providers.connection import ...`` calls
continue to work. New code should import from ``wren.providers`` directly.

Note: tests must monkeypatch ``wren.providers.connection.list_profiles`` /
``get_active_profile`` - patching this shim no longer affects the real logic.
"""

from wren.profile import (  # noqa: F401  re-exported for backward compat
    expand_profile_secrets,
    get_active_profile,
    list_profiles,
)
from wren.providers.connection import ProfileConnectionProvider
