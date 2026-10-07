"""Every calendar tool must be listed in core/tool_tiers.yaml, or
core/tool_registry.py:filter_server_tools silently prunes it at startup."""

import os
import sys

import yaml

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

import gcalendar.calendar_tools  # noqa: F401  (registers tools as an import side effect)
from core.server import server
from core.tool_registry import get_tool_components


def test_every_calendar_tool_is_listed_in_tool_tiers_yaml():
    tool_components = get_tool_components(server)

    calendar_tool_names = set()
    for name, component in tool_components.items():
        func = getattr(component, "fn", component)
        if getattr(func, "__module__", "") == "gcalendar.calendar_tools":
            calendar_tool_names.add(name)

    assert {"manage_calendar", "manage_calendar_acl", "get_calendar_settings"} <= (
        calendar_tool_names
    )

    yaml_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "core", "tool_tiers.yaml"
    )
    with open(yaml_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    listed = set()
    for tier in ("core", "extended", "complete"):
        listed.update(config.get("calendar", {}).get(tier) or [])

    missing = calendar_tool_names - listed
    assert not missing, (
        "Tool(s) registered in gcalendar/calendar_tools.py but missing from "
        f"core/tool_tiers.yaml's calendar section: {sorted(missing)}"
    )
