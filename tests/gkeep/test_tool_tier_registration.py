"""Every keep tool must be listed in core/tool_tiers.yaml, or
core/tool_registry.py:filter_server_tools silently prunes it at startup."""

import os
import sys

import yaml

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

import gkeep.keep_tools  # noqa: F401  (registers tools as an import side effect)
from auth.permissions import get_scopes_for_permission
from auth.scopes import get_all_read_only_scopes
from core.server import server
from core.tool_registry import get_tool_components

EXPECTED_TOOLS = {
    "list_keep_notes",
    "get_keep_note",
    "manage_keep_note",
    "manage_keep_note_permissions",
}


def _keep_tool_names():
    names = set()
    for name, component in get_tool_components(server).items():
        func = getattr(component, "fn", component)
        if getattr(func, "__module__", "") == "gkeep.keep_tools":
            names.add(name)
    return names


def test_every_gkeep_tool_is_listed_in_tool_tiers_yaml():
    keep_tool_names = _keep_tool_names()
    assert keep_tool_names, (
        "No gkeep tools found registered on the shared server - "
        "this test's own detection is broken, not a real pass."
    )

    yaml_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "core", "tool_tiers.yaml"
    )
    with open(yaml_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    listed = set()
    for tier in ("core", "extended", "complete"):
        listed.update(config.get("keep", {}).get(tier) or [])

    missing = keep_tool_names - listed
    assert not missing, (
        "Tool(s) registered in gkeep/keep_tools.py but missing from "
        "core/tool_tiers.yaml's keep section (they will be silently pruned "
        f"at startup under any TOOL_TIER/TOOLS filtering): {sorted(missing)}"
    )


def test_exactly_the_four_expected_tools_are_registered():
    assert _keep_tool_names() == EXPECTED_TOOLS


def _required_scopes(tool_name):
    component = get_tool_components(server)[tool_name]
    func = getattr(component, "fn", component)
    return set(getattr(func, "_required_google_scopes", []))


def test_keep_read_tools_survive_read_only_filtering():
    allowed = set(get_all_read_only_scopes())
    for tool_name in ("list_keep_notes", "get_keep_note"):
        assert _required_scopes(tool_name), tool_name
        assert _required_scopes(tool_name) <= allowed, tool_name


def test_keep_mutation_tools_survive_full_permission_filtering():
    allowed = set(get_scopes_for_permission("keep", "full"))
    for tool_name in ("manage_keep_note", "manage_keep_note_permissions"):
        assert _required_scopes(tool_name), tool_name
        assert _required_scopes(tool_name) <= allowed, tool_name
