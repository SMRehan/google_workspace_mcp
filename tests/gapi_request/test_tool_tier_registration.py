"""Every gapi_request tool must be listed in core/tool_tiers.yaml, or
core/tool_registry.py:filter_server_tools silently prunes it at startup."""

import os
import sys

import yaml

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

import gapi_request.api_request_tools  # noqa: F401  (registers tools as an import side effect)
from core.server import server
from core.tool_registry import get_tool_components


def _registered_api_tool_names():
    names = set()
    for name, component in get_tool_components(server).items():
        func = getattr(component, "fn", component)
        if getattr(func, "__module__", "") == "gapi_request.api_request_tools":
            names.add(name)
    return names


def test_google_api_request_is_registered():
    assert "google_api_request" in _registered_api_tool_names()


def test_every_gapi_request_tool_is_listed_in_tool_tiers_yaml():
    tool_names = _registered_api_tool_names()
    assert tool_names, (
        "No gapi_request tools found registered on the shared server - "
        "this test's own detection is broken, not a real pass."
    )

    yaml_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "core", "tool_tiers.yaml"
    )
    with open(yaml_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    listed = set()
    for tier in ("core", "extended", "complete"):
        listed.update(config.get("api", {}).get(tier) or [])

    missing = tool_names - listed
    assert not missing, (
        "Tool(s) registered in gapi_request/api_request_tools.py but missing from "
        f"core/tool_tiers.yaml's api section: {sorted(missing)}"
    )


def test_annotations_mark_the_tool_as_open_ended_and_destructive():
    component = get_tool_components(server)["google_api_request"]
    annotations = component.annotations
    assert annotations.read_only_hint is False
    assert annotations.destructive_hint is True
    assert annotations.idempotent_hint is False
    assert annotations.open_world_hint is True


def test_docstring_explains_method_syntax_and_guard_rail_bypass():
    component = get_tool_components(server)["google_api_request"]
    doc = component.fn.__doc__
    for needle in (
        "events.move",
        "users.drafts.list",
        "spreadsheets.values.append",
        "documents.batchUpdate",
        "_media",
        "guard rails",
    ):
        assert needle in doc, needle
