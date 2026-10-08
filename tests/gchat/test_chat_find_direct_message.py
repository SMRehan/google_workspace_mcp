"""Unit tests for find_chat_direct_message (spaces.findDirectMessage)."""

import os
import sys
from unittest.mock import Mock

import pytest
from googleapiclient.errors import HttpError
from httplib2 import Response

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from core.server import server
from core.tool_registry import get_tool_components
from core.utils import UserInputError
from gchat.chat_tools import find_chat_direct_message

EMAIL = "me@example.com"


def _unwrap(tool):
    fn = getattr(tool, "fn", tool)
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


async def _run(service, **kwargs):
    return await _unwrap(find_chat_direct_message)(
        service=service, user_google_email=EMAIL, **kwargs
    )


@pytest.mark.asyncio
async def test_existing_dm_reports_space_name():
    service = Mock()
    service.spaces().findDirectMessage().execute.return_value = {
        "name": "spaces/DM1",
        "spaceType": "DIRECT_MESSAGE",
        "spaceUri": "https://chat.google.com/dm/DM1",
    }
    result = await _run(service, user="ann@example.com")
    assert "exists: true" in result
    assert "Space ID: spaces/DM1" in result
    assert "- URL: https://chat.google.com/dm/DM1" in result
    service.spaces().findDirectMessage.assert_called_with(name="users/ann@example.com")


@pytest.mark.asyncio
async def test_users_id_passed_through():
    service = Mock()
    service.spaces().findDirectMessage().execute.return_value = {"name": "spaces/DM1"}
    await _run(service, user="users/123")
    service.spaces().findDirectMessage.assert_called_with(name="users/123")


@pytest.mark.asyncio
async def test_missing_dm_reports_not_exists():
    service = Mock()
    service.spaces().findDirectMessage().execute.side_effect = HttpError(
        resp=Response({"status": 404}), content=b'{"error": {"code": 404}}'
    )
    result = await _run(service, user="ann@example.com")
    assert "exists: false" in result
    assert "No direct message exists" in result
    assert "action='setup'" in result


@pytest.mark.asyncio
async def test_other_http_errors_propagate():
    service = Mock()
    service.spaces().findDirectMessage().execute.side_effect = HttpError(
        resp=Response({"status": 403}), content=b"forbidden"
    )
    with pytest.raises(HttpError):
        await _run(service, user="ann@example.com")


@pytest.mark.asyncio
async def test_blank_user_rejected():
    service = Mock()
    with pytest.raises(UserInputError, match="'user' is required"):
        await _run(service, user="  ")
    service.spaces.assert_not_called()


def test_tool_is_read_only():
    # `server` is bound at import time: other tests replace core.server.server
    # with a fresh instance that never saw the Chat tools register.
    tool = get_tool_components(server)["find_chat_direct_message"]
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.destructive_hint is False
