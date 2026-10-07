"""Every meet tool must be listed in core/tool_tiers.yaml, or
core/tool_registry.py:filter_server_tools silently prunes it at startup."""

import os
import sys

import yaml

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

import gmeet.meet_tools  # noqa: F401  (registers tools as an import side effect)
from auth.permissions import get_scopes_for_permission
from auth.scopes import get_all_read_only_scopes
from core.server import server
from core.tool_registry import get_tool_components

EXPECTED_TOOLS = {
    "manage_meet_space",
    "manage_meet_member",
    "list_meet_conference_records",
    "get_meet_conference_artifacts",
}


def _registered_meet_tools():
    tool_components = get_tool_components(server)
    names = set()
    for name, component in tool_components.items():
        func = getattr(component, "fn", component)
        if getattr(func, "__module__", "") == "gmeet.meet_tools":
            names.add(name)
    return names


def test_the_four_meet_tools_are_registered():
    assert _registered_meet_tools() == EXPECTED_TOOLS


def test_every_gmeet_tool_is_listed_in_tool_tiers_yaml():
    gmeet_tool_names = _registered_meet_tools()

    assert gmeet_tool_names, (
        "No gmeet tools found registered on the shared server - "
        "this test's own detection is broken, not a real pass."
    )

    yaml_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "core", "tool_tiers.yaml"
    )
    with open(yaml_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    listed = set()
    for tier in ("core", "extended", "complete"):
        listed.update(config.get("meet", {}).get(tier) or [])

    missing = gmeet_tool_names - listed
    assert not missing, (
        "Tool(s) registered in gmeet/meet_tools.py but missing from "
        "core/tool_tiers.yaml's meet section (they will be silently pruned "
        f"at startup under any TOOL_TIER/TOOLS filtering): {sorted(missing)}"
    )

    stale = listed - gmeet_tool_names
    assert not stale, (
        "core/tool_tiers.yaml's meet section lists tool(s) that gmeet/meet_tools.py "
        f"does not register: {sorted(stale)}"
    )


def _required_scopes(tool_name):
    component = get_tool_components(server)[tool_name]
    func = getattr(component, "fn", component)
    return set(getattr(func, "_required_google_scopes", []))


def test_meet_read_tools_survive_read_only_filtering():
    allowed = set(get_all_read_only_scopes())
    read_tools = {"list_meet_conference_records", "get_meet_conference_artifacts"}

    for tool_name in read_tools:
        assert _required_scopes(tool_name), tool_name
        assert _required_scopes(tool_name) <= allowed, tool_name


def test_meet_mutation_tools_are_pruned_in_read_only_mode():
    allowed = set(get_all_read_only_scopes())

    for tool_name in ("manage_meet_space", "manage_meet_member"):
        assert not _required_scopes(tool_name) <= allowed, tool_name


def test_meet_mutation_tools_survive_full_permission_filtering():
    allowed = set(get_scopes_for_permission("meet", "full"))

    for tool_name in EXPECTED_TOOLS:
        assert _required_scopes(tool_name) <= allowed, tool_name
