"""
Unit tests for google_api_request.

The Google side is a hand-built stand-in for googleapiclient's Resource tree
(discovery description on ``_resourceDesc``, one attribute per collection and
method, ``*_media`` variants where the description allows them). The auth side
patches ``auth.service_decorator._authenticate_service`` so the real
``require_google_service`` wrapper runs end to end without network or
credentials, exactly as the typed tools are tested.
"""

import base64
import json
import os
import sys
from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import Mock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

import auth.scopes as scopes_module
import auth.service_decorator as service_decorator
from auth.permissions import get_scopes_for_permission, set_permissions
from auth.scopes import (
    TOOL_READONLY_SCOPES_MAP,
    TOOL_SCOPES_MAP,
    set_read_only,
)
from core.server import server
from core.tool_registry import get_tool_components
from core.utils import UserInputError
from gapi_request import api_request_helpers as helpers
import gapi_request.api_request_tools  # noqa: F401  (registers the tool)

EMAIL = "user@example.com"


# ---------------------------------------------------------------------------
# Fake discovery resources
# ---------------------------------------------------------------------------


class FakeRequest:
    """Stands in for googleapiclient.http.HttpRequest."""

    def __init__(
        self,
        kwargs: Dict[str, Any],
        payload: Any,
        status: Optional[int] = 200,
        media: bool = False,
    ):
        self.kwargs = kwargs
        self.payload = payload
        self.status = status
        self.media = media
        self.method = "GET"
        self.uri = "https://example.invalid/"
        self.postproc = lambda resp, content: content
        self.executed = 0

    def execute(self, http=None, num_retries=0):
        self.executed += 1
        if self.status is None:
            # Simulate a request whose postproc is never consulted.
            return self.payload
        return self.postproc(SimpleNamespace(status=self.status), self.payload)


class FakeResource:
    """Minimal googleapiclient Resource: methods and nested collections."""

    def __init__(
        self,
        desc: Dict[str, Any],
        payloads: Dict[str, Any],
        prefix: str = "",
        built: Optional[list] = None,
    ):
        self._resourceDesc = desc
        self._payloads = payloads
        self._prefix = prefix
        self.built: list = [] if built is None else built
        for name, sub in (desc.get("resources") or {}).items():
            setattr(self, name, self._collection(name, sub))
        for name, method_desc in (desc.get("methods") or {}).items():
            if not method_desc.get("useMediaDownloadOnly"):
                setattr(self, name, self._method(name, media=False))
            if method_desc.get("supportsMediaDownload"):
                setattr(self, name + "_media", self._method(name, media=True))

    def _collection(self, name, sub):
        child = FakeResource(sub, self._payloads, f"{self._prefix}{name}.", self.built)
        return lambda: child

    def _method(self, name, media):
        full = f"{self._prefix}{name}" + ("_media" if media else "")

        def call(**kwargs):
            payload, status = self._payloads.get(full, ({"ok": True}, 200))
            request = FakeRequest(kwargs, payload, status=status, media=media)
            self.built.append((full, request))
            return request

        return call


class FakeService(FakeResource):
    def __init__(self, desc, payloads=None):
        super().__init__(desc, payloads or {})
        self._http = SimpleNamespace(http=Mock(name="pooled-http"))
        self.closed = False

    def close(self):
        self.closed = True


def _m(http_method, **extra):
    desc = {"httpMethod": http_method, "parameters": {"calendarId": {}, "eventId": {}}}
    desc.update(extra)
    return desc


CALENDAR_DESC = {
    "resources": {
        "events": {
            "methods": {
                "list": _m("GET"),
                "get": _m("GET"),
                "move": _m("POST"),
                "delete": _m("DELETE"),
                "insert": _m("POST", request={"$ref": "Event"}),
            }
        },
        "calendarList": {"methods": {"list": _m("GET")}},
    },
    "methods": {},
}

DRIVE_DESC = {
    "resources": {
        "files": {
            "methods": {
                "list": _m("GET"),
                "get": _m("GET", supportsMediaDownload=True),
                "export": _m(
                    "GET", supportsMediaDownload=True, useMediaDownloadOnly=True
                ),
            }
        }
    }
}

GMAIL_DESC = {
    "resources": {
        "users": {
            "methods": {"getProfile": _m("GET")},
            "resources": {
                "messages": {"methods": {"list": _m("GET"), "send": _m("POST")}},
                "drafts": {"methods": {"list": _m("GET")}},
            },
        }
    }
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_policy_state():
    scopes_module._ENABLED_TOOLS = None
    set_read_only(False)
    set_permissions(None)
    yield
    scopes_module._ENABLED_TOOLS = None
    set_read_only(False)
    set_permissions(None)


@pytest.fixture
def auth(monkeypatch):
    """Patch the decorator's auth seam; return a recorder of what it was asked for."""
    recorder = SimpleNamespace(calls=[], service=None)

    async def fake_get_auth_context(tool_name):
        return (None, None, None)

    async def fake_authenticate_service(
        use_oauth21,
        service_name,
        service_version,
        tool_name,
        user_google_email,
        resolved_scopes,
        mcp_session_id,
        authenticated_user,
    ):
        recorder.calls.append(
            {
                "service_name": service_name,
                "version": service_version,
                "tool_name": tool_name,
                "email": user_google_email,
                "scopes": list(resolved_scopes),
            }
        )
        return recorder.service, user_google_email

    monkeypatch.setattr(service_decorator, "is_oauth21_enabled", lambda: False)
    monkeypatch.setattr(service_decorator, "_user_email_is_managed", lambda: False)
    monkeypatch.setattr(service_decorator, "_get_auth_context", fake_get_auth_context)
    monkeypatch.setattr(
        service_decorator, "_detect_oauth_version", lambda *a, **k: False
    )
    monkeypatch.setattr(
        service_decorator, "_authenticate_service", fake_authenticate_service
    )
    return recorder


def _tool():
    return get_tool_components(server)["google_api_request"].fn


async def _call(**kwargs):
    kwargs.setdefault("user_google_email", EMAIL)
    return await _tool()(**kwargs)


def _body_json(result: str) -> Any:
    header, _, body = result.partition("\n")
    return json.loads(body)


# ---------------------------------------------------------------------------
# Service resolution and policy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, expected",
    [
        ("gmail", ("gmail", "gmail")),
        ("contacts", ("people", "contacts")),
        ("people", ("people", "contacts")),
        ("appscript", ("script", "appscript")),
        ("script", ("script", "appscript")),
        ("search", ("customsearch", "search")),
        ("customsearch", ("customsearch", "search")),
        (" Calendar ", ("calendar", "calendar")),
    ],
)
def test_resolve_service_accepts_config_keys_and_tool_aliases(name, expected):
    assert helpers.resolve_service(name) == expected


def test_resolve_service_rejects_unknown_name_and_lists_options():
    with pytest.raises(UserInputError) as exc:
        helpers.resolve_service("youtube")
    message = str(exc.value)
    assert "youtube" in message
    for name in ("gmail", "people", "contacts", "customsearch", "appscript"):
        assert name in message


def test_resolve_version_defaults_and_overrides():
    assert helpers.resolve_version("drive", None) == "v3"
    assert helpers.resolve_version("drive", "") == "v3"
    assert helpers.resolve_version("drive", "v2") == "v2"
    assert helpers.resolve_version("meet", "v2beta") == "v2beta"
    with pytest.raises(UserInputError):
        helpers.resolve_version("drive", "../etc")


def test_ensure_service_enabled_allows_everything_when_unrestricted():
    helpers.ensure_service_enabled("gmail", "gmail")


def test_ensure_service_enabled_refuses_and_lists_enabled_services():
    scopes_module._ENABLED_TOOLS = ["api", "gmail", "contacts"]
    helpers.ensure_service_enabled("gmail", "gmail")
    helpers.ensure_service_enabled("contacts", "people")
    with pytest.raises(UserInputError) as exc:
        helpers.ensure_service_enabled("calendar", "calendar")
    message = str(exc.value)
    assert "calendar" in message
    assert "gmail" in message and "contacts" in message
    assert "--tools calendar" in message


def test_required_scopes_follow_server_policy():
    assert helpers.required_scopes_for("gmail") == TOOL_SCOPES_MAP["gmail"]
    set_read_only(True)
    assert helpers.required_scopes_for("gmail") == TOOL_READONLY_SCOPES_MAP["gmail"]
    set_read_only(False)
    set_permissions({"gmail": "organize"})
    assert helpers.required_scopes_for("gmail") == get_scopes_for_permission(
        "gmail", "organize"
    )


def test_is_service_read_only_honours_global_mode_and_permission_level():
    assert helpers.is_service_read_only("drive") is False
    set_read_only(True)
    assert helpers.is_service_read_only("drive") is True
    set_read_only(False)
    set_permissions({"drive": "readonly", "gmail": "send"})
    assert helpers.is_service_read_only("drive") is True
    assert helpers.is_service_read_only("gmail") is False


# ---------------------------------------------------------------------------
# Method walking
# ---------------------------------------------------------------------------


def test_walk_method_resolves_nested_collections():
    service = FakeService(GMAIL_DESC)
    bound, desc, is_media = helpers.walk_method(service, "users.messages.list")
    assert desc["httpMethod"] == "GET"
    assert is_media is False
    request = bound(userId="me")
    assert service.built == [("users.messages.list", request)]


def test_walk_method_resolves_media_variants():
    service = FakeService(DRIVE_DESC)
    _bound, desc, is_media = helpers.walk_method(service, "files.get_media")
    assert is_media and desc["supportsMediaDownload"]
    _bound, _desc, is_media = helpers.walk_method(service, "files.export_media")
    assert is_media


def test_walk_method_unknown_collection_lists_available_collections():
    with pytest.raises(UserInputError) as exc:
        helpers.walk_method(FakeService(CALENDAR_DESC), "event.list")
    message = str(exc.value)
    assert "'event' is not a collection" in message
    assert "collections: calendarList, events" in message


def test_walk_method_unknown_method_lists_available_methods():
    with pytest.raises(UserInputError) as exc:
        helpers.walk_method(FakeService(CALENDAR_DESC), "events.patch")
    message = str(exc.value)
    assert "'patch' is not a method" in message
    assert "under 'events'" in message
    assert "methods: delete, get, insert, list, move" in message


def test_walk_method_lists_media_methods_the_client_exposes():
    with pytest.raises(UserInputError) as exc:
        helpers.walk_method(FakeService(DRIVE_DESC), "files.download")
    message = str(exc.value)
    assert "export_media" in message
    assert "get_media" in message
    assert "get," in message  # plain get exists, export does not
    assert "export," not in message


def test_walk_method_refuses_media_suffix_without_media_support():
    with pytest.raises(UserInputError):
        helpers.walk_method(FakeService(CALENDAR_DESC), "events.list_media")


@pytest.mark.parametrize(
    "bad", ["", "events..list", "_http.request", "events.list()", "a.b.c.d.e.f.g"]
)
def test_split_method_path_rejects_unsafe_or_malformed_paths(bad):
    with pytest.raises(UserInputError):
        helpers.split_method_path(bad)


def test_walk_method_without_description_falls_back_to_getattr():
    service = Mock()
    bound, desc, _ = helpers.walk_method(service, "tasklists.list")
    assert desc is None
    assert bound is service.tasklists.return_value.list


# ---------------------------------------------------------------------------
# Read-only enforcement
# ---------------------------------------------------------------------------


def test_read_only_allows_get_and_refuses_everything_else():
    helpers.ensure_read_only_allows("calendar", "events.list", {"httpMethod": "GET"})
    with pytest.raises(UserInputError) as exc:
        helpers.ensure_read_only_allows(
            "calendar", "events.move", {"httpMethod": "POST"}
        )
    assert "HTTP POST" in str(exc.value)


def test_read_only_fails_closed_when_verb_is_unknown():
    with pytest.raises(UserInputError) as exc:
        helpers.ensure_read_only_allows("tasks", "tasklists.list", None)
    assert "could not be determined" in str(exc.value)


# ---------------------------------------------------------------------------
# Request construction
# ---------------------------------------------------------------------------


def test_build_request_passes_params_and_body():
    captured = {}

    def bound(**kwargs):
        captured.update(kwargs)
        return "request"

    helpers.build_request(
        bound, "x.y", {"calendarId": "primary"}, {"summary": "Hi"}, None
    )
    assert captured == {"calendarId": "primary", "body": {"summary": "Hi"}}


def test_build_request_omits_body_when_none():
    captured = {}

    def bound(**kwargs):
        captured.update(kwargs)

    helpers.build_request(bound, "x.y", {"a": 1}, None, None)
    assert captured == {"a": 1}


def test_build_request_rejects_double_body_and_uploads():
    with pytest.raises(UserInputError):
        helpers.build_request(lambda **k: None, "x.y", {"body": {}}, {"a": 1}, None)
    with pytest.raises(UserInputError) as exc:
        helpers.build_request(lambda **k: None, "x.y", {"media_body": "f"}, None, None)
    assert "media uploads" in str(exc.value)


def test_build_request_surfaces_accepted_parameters_on_type_error():
    def bound(**kwargs):
        raise TypeError("Got an unexpected keyword argument 'calendar'")

    desc = {"parameters": {"calendarId": {}, "eventId": {}}, "request": {}}
    with pytest.raises(UserInputError) as exc:
        helpers.build_request(bound, "events.move", {"calendar": "x"}, None, desc)
    message = str(exc.value)
    assert "unexpected keyword argument" in message
    assert "accepted params: calendarId, eventId" in message
    assert "accepts a body" in message


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_format_result_has_header_and_pretty_json():
    out = helpers.format_result("calendar", "events.list", {"items": []}, 200, 1000)
    assert out.splitlines()[0] == "calendar.events.list → HTTP 200"
    assert _body_json(out) == {"items": []}


def test_format_result_marks_empty_bodies_and_unknown_status():
    out = helpers.format_result("calendar", "events.delete", "", None, 1000)
    assert out == "calendar.events.delete → HTTP ok\n(empty response body)"


def test_truncate_adds_note_with_counts():
    out = helpers.truncate("x" * 100, 40)
    assert out.startswith("x" * 40)
    assert "showing 40 of 100 characters" in out
    assert helpers.truncate("short", 40) == "short"


def test_resolve_max_output_chars():
    assert helpers.resolve_max_output_chars(None) == 60_000
    assert helpers.resolve_max_output_chars(10) == 10
    with pytest.raises(UserInputError):
        helpers.resolve_max_output_chars(0)


def test_describe_media_without_base64():
    summary = helpers.describe_media(b"%PDF-1.4 ...", None, False, 1000)
    assert summary["bytes"] == 12
    assert summary["mime_type_guess"] == "application/pdf"
    assert "base64" not in summary and "return_base64" in summary["note"]


def test_describe_media_with_base64_and_mime_param():
    data = b"\x00\x01binary"
    summary = helpers.describe_media(data, {"mimeType": "image/webp"}, True, 1000)
    assert summary["mime_type_guess"] == "image/webp"
    assert base64.b64decode(summary["base64"]) == data


def test_describe_media_omits_base64_that_exceeds_output_cap():
    summary = helpers.describe_media(b"a" * 300, None, True, 100)
    assert "base64" not in summary
    assert "exceed max_output_chars" in summary["note"]
    assert summary["mime_type_guess"] == "text/plain"


# ---------------------------------------------------------------------------
# End to end through the registered tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_moves_event_with_params_and_reports_status(auth):
    auth.service = FakeService(
        CALENDAR_DESC, {"events.move": ({"id": "abc", "status": "confirmed"}, 200)}
    )
    out = await _call(
        service="calendar",
        method="events.move",
        params={"calendarId": "primary", "eventId": "abc", "destination": "d"},
    )
    assert out.splitlines()[0] == "calendar.events.move → HTTP 200"
    assert _body_json(out) == {"id": "abc", "status": "confirmed"}

    full, request = auth.service.built[0]
    assert full == "events.move"
    assert request.kwargs == {
        "calendarId": "primary",
        "eventId": "abc",
        "destination": "d",
    }
    assert request.executed == 1

    (call,) = auth.calls
    assert call["service_name"] == "calendar"
    assert call["version"] == "v3"
    assert call["tool_name"] == "google_api_request"
    assert call["email"] == EMAIL
    assert set(call["scopes"]) == set(TOOL_SCOPES_MAP["calendar"])


@pytest.mark.asyncio
async def test_tool_passes_body_and_version_override(auth):
    auth.service = FakeService(CALENDAR_DESC)
    await _call(
        service="calendar",
        method="events.insert",
        params={"calendarId": "primary"},
        body={"summary": "Lunch"},
        version="v3beta",
    )
    _full, request = auth.service.built[0]
    assert request.kwargs == {"calendarId": "primary", "body": {"summary": "Lunch"}}
    assert auth.calls[0]["version"] == "v3beta"


@pytest.mark.asyncio
async def test_tool_resolves_alias_and_scopes_by_tool_key(auth):
    auth.service = FakeService(GMAIL_DESC)
    out = await _call(
        service="gmail", method="users.drafts.list", params={"userId": "me"}
    )
    assert out.startswith("gmail.users.drafts.list → HTTP 200")

    auth.service = FakeService(
        {"resources": {"people": {"methods": {"get": _m("GET")}}}}
    )
    await _call(service="contacts", method="people.get", params={"resourceName": "x"})
    assert auth.calls[-1]["service_name"] == "people"
    assert set(auth.calls[-1]["scopes"]) == set(TOOL_SCOPES_MAP["contacts"])


@pytest.mark.asyncio
async def test_tool_refuses_disabled_service_before_authenticating(auth):
    scopes_module._ENABLED_TOOLS = ["api", "gmail"]
    auth.service = FakeService(CALENDAR_DESC)
    with pytest.raises(UserInputError) as exc:
        await _call(
            service="calendar", method="events.list", params={"calendarId": "p"}
        )
    assert "not enabled" in str(exc.value)
    assert auth.calls == []


@pytest.mark.asyncio
async def test_tool_in_read_only_mode_allows_get_and_refuses_post(auth):
    set_read_only(True)
    auth.service = FakeService(CALENDAR_DESC, {"events.list": ({"items": []}, 200)})
    out = await _call(
        service="calendar", method="events.list", params={"calendarId": "p"}
    )
    assert _body_json(out) == {"items": []}
    assert set(auth.calls[0]["scopes"]) == set(TOOL_READONLY_SCOPES_MAP["calendar"])

    with pytest.raises(UserInputError) as exc:
        await _call(
            service="calendar",
            method="events.move",
            params={"calendarId": "p", "eventId": "e", "destination": "d"},
        )
    assert "only GET methods" in str(exc.value)
    assert all(full == "events.list" for full, _ in auth.service.built)


@pytest.mark.asyncio
async def test_tool_honours_readonly_permission_level_per_service(auth):
    set_permissions({"gmail": "readonly", "calendar": "full"})
    auth.service = FakeService(GMAIL_DESC)
    with pytest.raises(UserInputError):
        await _call(
            service="gmail", method="users.messages.send", params={"userId": "me"}
        )
    assert set(auth.calls[0]["scopes"]) == set(
        get_scopes_for_permission("gmail", "readonly")
    )

    auth.service = FakeService(CALENDAR_DESC)
    await _call(
        service="calendar",
        method="events.move",
        params={"calendarId": "p", "eventId": "e", "destination": "d"},
    )
    assert auth.service.built[0][0] == "events.move"


@pytest.mark.asyncio
async def test_tool_unknown_method_error_lists_options(auth):
    auth.service = FakeService(GMAIL_DESC)
    with pytest.raises(UserInputError) as exc:
        await _call(service="gmail", method="users.message.list", params={})
    message = str(exc.value)
    assert "'message' is not a collection" in message
    assert "collections: drafts, messages" in message
    assert "methods: getProfile" in message


@pytest.mark.asyncio
async def test_tool_media_path_returns_summary_and_optional_base64(auth, monkeypatch):
    data = b"\x89PNG\r\n\x1a\n" + bytes(range(32))
    seen = {}

    async def fake_download(request, **kwargs):
        seen["request"] = request
        seen["kwargs"] = kwargs
        return data

    monkeypatch.setattr(helpers, "download_media_bytes", fake_download)
    auth.service = FakeService(DRIVE_DESC)

    out = await _call(
        service="drive", method="files.get_media", params={"fileId": "f1"}
    )
    summary = _body_json(out)
    assert out.startswith("drive.files.get_media → HTTP ok")
    assert summary["bytes"] == len(data)
    assert summary["mime_type_guess"] == "image/png"
    assert "base64" not in summary
    assert seen["request"].kwargs == {"fileId": "f1"}
    assert seen["request"].executed == 0  # downloader owns execution

    out = await _call(
        service="drive",
        method="files.export_media",
        params={"fileId": "f1", "mimeType": "application/pdf"},
        return_base64=True,
    )
    summary = _body_json(out)
    assert summary["mime_type_guess"] == "application/pdf"
    assert base64.b64decode(summary["base64"]) == data


@pytest.mark.asyncio
async def test_tool_truncates_long_output(auth):
    big = {"items": [{"id": f"evt-{i}", "summary": "x" * 50} for i in range(200)]}
    auth.service = FakeService(CALENDAR_DESC, {"events.list": (big, 200)})
    out = await _call(
        service="calendar",
        method="events.list",
        params={"calendarId": "p"},
        max_output_chars=500,
    )
    header, _, body = out.partition("\n")
    assert header == "calendar.events.list → HTTP 200"
    assert "[Output truncated: showing 500 of" in body
    assert len(body) < 800


@pytest.mark.asyncio
async def test_tool_reports_ok_when_status_is_not_observable(auth):
    auth.service = FakeService(CALENDAR_DESC, {"events.delete": ("", None)})
    out = await _call(
        service="calendar",
        method="events.delete",
        params={"calendarId": "p", "eventId": "e"},
    )
    assert out == "calendar.events.delete → HTTP ok\n(empty response body)"


@pytest.mark.asyncio
async def test_tool_returns_connection_to_pool_like_typed_tools(auth):
    auth.service = FakeService(CALENDAR_DESC)
    await _call(service="calendar", method="calendarList.list", params={})
    assert auth.service.closed is False


@pytest.mark.asyncio
async def test_tool_surfaces_missing_scope_message_from_decorator(auth, monkeypatch):
    async def deny(*args, **kwargs):
        raise service_decorator.GoogleScopeError("need calendar")

    monkeypatch.setattr(service_decorator, "_authenticate_service", deny)
    out = await _call(
        service="calendar", method="events.list", params={"calendarId": "p"}
    )
    assert "Permission required for calendar" in out


# ---------------------------------------------------------------------------
# Managed-identity signature handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accept_managed_user_email_hides_parameter_when_managed(monkeypatch):
    import inspect

    async def tool(user_google_email: str, service: str) -> str:
        """Args:\n    user_google_email (str): who.\n    service (str): what.\n"""
        return f"{user_google_email}:{service}"

    monkeypatch.setattr(helpers, "_user_email_is_managed", lambda: False)
    assert helpers.accept_managed_user_email(tool) is tool

    monkeypatch.setattr(helpers, "_user_email_is_managed", lambda: True)
    wrapped = helpers.accept_managed_user_email(tool)
    assert list(inspect.signature(wrapped).parameters) == ["service"]
    assert "user_google_email" not in wrapped.__doc__
    assert await wrapped(service="gmail") == "None:gmail"
