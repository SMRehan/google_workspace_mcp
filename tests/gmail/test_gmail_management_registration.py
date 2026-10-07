"""Registration checks for the Gmail management tools.

Every new tool must be listed in core/tool_tiers.yaml or
core/tool_registry.py:filter_server_tools prunes it at startup, and the new
scope constants must be wired into the decorator's scope groups without
leaking into the default Gmail consent.
"""

import os
import sys

import yaml

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

import gmail.gmail_tools  # noqa: F401  (registers tools as an import side effect)
from auth.scopes import (
    GMAIL_FULL_SCOPE,
    GMAIL_SCOPES,
    GMAIL_SETTINGS_SHARING_SCOPE,
)
from auth.service_decorator import SCOPE_GROUPS
from core.server import server
from core.tool_registry import get_tool_components

MANAGEMENT_TOOLS = {
    "manage_gmail_message",
    "manage_gmail_draft",
    "manage_gmail_thread",
    "get_gmail_profile",
    "manage_gmail_settings",
    "manage_gmail_sharing_settings",
    "list_gmail_history",
}


def _gmail_tiers():
    yaml_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "core", "tool_tiers.yaml"
    )
    with open(yaml_path, encoding="utf-8") as f:
        return yaml.safe_load(f)["gmail"]


def test_importing_gmail_tools_registers_the_management_tools():
    registered = set(get_tool_components(server))
    assert MANAGEMENT_TOOLS <= registered


def test_management_tools_are_listed_in_gmail_complete_tier():
    tiers = _gmail_tiers()
    assert MANAGEMENT_TOOLS <= set(tiers["complete"])
    for tier in ("core", "extended"):
        assert not MANAGEMENT_TOOLS & set(tiers[tier])


def test_every_gmail_tool_is_listed_in_tool_tiers_yaml():
    tool_components = get_tool_components(server)
    gmail_tools = {
        name
        for name, component in tool_components.items()
        if getattr(getattr(component, "fn", component), "__module__", "").startswith(
            "gmail."
        )
    }
    assert gmail_tools
    tiers = _gmail_tiers()
    listed = set()
    for tier in ("core", "extended", "complete"):
        listed.update(tiers.get(tier) or [])
    assert gmail_tools <= listed


def test_new_scope_groups_resolve_to_the_new_constants():
    assert GMAIL_SETTINGS_SHARING_SCOPE == (
        "https://www.googleapis.com/auth/gmail.settings.sharing"
    )
    assert GMAIL_FULL_SCOPE == "https://mail.google.com/"
    assert SCOPE_GROUPS["gmail_settings_sharing"] == GMAIL_SETTINGS_SHARING_SCOPE
    assert SCOPE_GROUPS["gmail_full"] == GMAIL_FULL_SCOPE


def test_new_scopes_are_not_requested_by_default():
    assert GMAIL_SETTINGS_SHARING_SCOPE not in GMAIL_SCOPES
    assert GMAIL_FULL_SCOPE not in GMAIL_SCOPES
