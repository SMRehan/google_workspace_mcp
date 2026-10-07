"""Every gdrive tool must be listed in core/tool_tiers.yaml, or
core/tool_registry.py:filter_server_tools silently prunes it at startup."""

import os
import sys

import yaml

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

import gdrive.drive_tools  # noqa: F401  (registers tools as an import side effect)
from core.server import server
from core.tool_registry import get_tool_components

NEW_DRIVE_TOOLS = {
    "manage_drive_file",
    "get_drive_about",
    "manage_drive_revisions",
    "list_drive_changes",
    "manage_shared_drive",
}


def _listed_drive_tools():
    yaml_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "core", "tool_tiers.yaml"
    )
    with open(yaml_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    listed = set()
    for tier in ("core", "extended", "complete"):
        listed.update(config.get("drive", {}).get(tier) or [])
    return listed


def test_every_gdrive_tool_is_listed_in_tool_tiers_yaml():
    tool_components = get_tool_components(server)
    registered = {
        name
        for name, component in tool_components.items()
        if getattr(getattr(component, "fn", component), "__module__", "")
        == "gdrive.drive_tools"
    }
    assert NEW_DRIVE_TOOLS <= registered, NEW_DRIVE_TOOLS - registered

    missing = registered - _listed_drive_tools()
    assert not missing, f"gdrive tools missing from tool_tiers.yaml: {sorted(missing)}"


def test_new_drive_tools_are_in_complete_tier_only():
    yaml_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "core", "tool_tiers.yaml"
    )
    with open(yaml_path, encoding="utf-8") as f:
        drive = yaml.safe_load(f)["drive"]
    assert NEW_DRIVE_TOOLS <= set(drive["complete"])
    assert not NEW_DRIVE_TOOLS & set(drive["core"])
    assert not NEW_DRIVE_TOOLS & set(drive["extended"])
