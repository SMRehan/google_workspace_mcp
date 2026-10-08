"""Every gchat tool must be listed in core/tool_tiers.yaml and request scopes
the chat service actually asks for, or it is pruned or unauthenticable."""

import os
import sys

import pytest
import yaml

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

import gchat.chat_tools  # noqa: F401  (registers tools as an import side effect)
from auth.permissions import get_scopes_for_permission
from auth.scopes import (
    CHAT_DELETE_SCOPE,
    CHAT_MEMBERSHIPS_SCOPE,
    CHAT_SPACES_READONLY_SCOPE,
    CHAT_SPACES_SCOPE,
    CHAT_WRITE_SCOPE,
    SCOPE_HIERARCHY,
    CHAT_MEMBERSHIPS_READONLY_SCOPE,
    get_scopes_for_tools,
    has_required_scopes,
)
from core.server import server
from core.tool_registry import get_tool_components

NEW_TOOLS = {
    "manage_chat_member": [CHAT_MEMBERSHIPS_SCOPE],
    "manage_chat_message": [CHAT_WRITE_SCOPE],
    "manage_chat_reaction": [CHAT_WRITE_SCOPE],
    "find_chat_direct_message": [CHAT_SPACES_READONLY_SCOPE],
    "manage_chat_space": [CHAT_SPACES_SCOPE, CHAT_DELETE_SCOPE],
}


def _chat_tool_names():
    names = set()
    for name, component in get_tool_components(server).items():
        func = getattr(component, "fn", component)
        if getattr(func, "__module__", "") == "gchat.chat_tools":
            names.add(name)
    return names


def _required_scopes(tool_name):
    component = get_tool_components(server)[tool_name]
    func = getattr(component, "fn", component)
    return list(getattr(func, "_required_google_scopes", []))


def test_every_gchat_tool_is_listed_in_tool_tiers_yaml():
    names = _chat_tool_names()
    assert NEW_TOOLS.keys() <= names
    yaml_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "core", "tool_tiers.yaml"
    )
    with open(yaml_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    listed = set()
    for tier in ("core", "extended", "complete"):
        listed.update(config.get("chat", {}).get(tier) or [])
    assert not (names - listed), sorted(names - listed)
    assert NEW_TOOLS.keys() <= set(config["chat"]["complete"])


@pytest.mark.parametrize("tool_name", sorted(NEW_TOOLS))
def test_new_tools_request_expected_scopes(tool_name):
    assert _required_scopes(tool_name) == NEW_TOOLS[tool_name]


@pytest.mark.parametrize("tool_name", sorted(NEW_TOOLS))
def test_chat_service_and_full_permission_cover_new_tools(tool_name):
    required = _required_scopes(tool_name)
    assert has_required_scopes(get_scopes_for_tools(["chat"]), required)
    assert has_required_scopes(get_scopes_for_permission("chat", "full"), required)


def test_readonly_permission_covers_find_direct_message_only():
    readonly = get_scopes_for_permission("chat", "readonly")
    assert has_required_scopes(readonly, _required_scopes("find_chat_direct_message"))
    for tool_name in ("manage_chat_member", "manage_chat_space"):
        assert not has_required_scopes(readonly, _required_scopes(tool_name))


def test_memberships_scope_covers_readonly_variant():
    assert CHAT_MEMBERSHIPS_READONLY_SCOPE in SCOPE_HIERARCHY[CHAT_MEMBERSHIPS_SCOPE]
