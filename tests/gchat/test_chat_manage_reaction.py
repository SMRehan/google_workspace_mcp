"""Unit tests for manage_chat_reaction (spaces.messages.reactions list/delete)."""

import os
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from core.utils import UserInputError
from gchat.chat_tools import manage_chat_reaction

EMAIL = "me@example.com"
MSG = "spaces/S/messages/M"
REACTION = f"{MSG}/reactions/R"


def _unwrap(tool):
    fn = getattr(tool, "fn", tool)
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


async def _run(service, **kwargs):
    return await _unwrap(manage_chat_reaction)(
        service=service, user_google_email=EMAIL, **kwargs
    )


@pytest.mark.asyncio
async def test_list_reactions_formats_and_pages():
    service = Mock()
    service.spaces().messages().reactions().list().execute.return_value = {
        "reactions": [
            {
                "name": REACTION,
                "emoji": {"unicode": "👍"},
                "user": {"displayName": "Ann"},
            },
            {
                "name": f"{MSG}/reactions/R2",
                "emoji": {"customEmoji": {"uid": "party"}},
                "user": {"name": "users/2"},
            },
        ],
        "nextPageToken": "tok",
    }
    result = await _run(service, action="list", message_name=MSG)
    assert "Found 2 reaction(s)" in result
    assert f"- 👍 by Ann (ID: {REACTION})" in result
    assert "- :party: by users/2" in result
    assert "page_token='tok'" in result
    service.spaces().messages().reactions().list.assert_called_with(
        parent=MSG, pageSize=25
    )


@pytest.mark.asyncio
async def test_list_reactions_filter_by_emoji_and_user():
    service = Mock()
    service.spaces().messages().reactions().list().execute.return_value = {
        "reactions": []
    }
    result = await _run(
        service,
        action="list",
        message_name=MSG,
        emoji_unicode="👍",
        user="users/1",
        page_token="tok",
        page_size=5,
    )
    assert "No reactions found" in result
    service.spaces().messages().reactions().list.assert_called_with(
        parent=MSG,
        pageSize=5,
        filter='emoji.unicode = "👍" AND user.name = "users/1"',
        pageToken="tok",
    )


@pytest.mark.asyncio
async def test_list_reactions_user_email_becomes_users_alias():
    service = Mock()
    service.spaces().messages().reactions().list().execute.return_value = {
        "reactions": []
    }
    await _run(service, action="list", message_name=MSG, user="ann@example.com")
    service.spaces().messages().reactions().list.assert_called_with(
        parent=MSG, pageSize=25, filter='user.name = "users/ann@example.com"'
    )


@pytest.mark.asyncio
async def test_delete_reaction():
    service = Mock()
    result = await _run(service, action="delete", reaction_name=REACTION)
    assert result == f"Deleted reaction {REACTION}."
    service.spaces().messages().reactions().delete.assert_called_with(name=REACTION)


@pytest.mark.asyncio
async def test_delete_denied_under_permission_level(monkeypatch):
    import gchat.chat_helpers as helpers

    monkeypatch.setattr(helpers, "is_action_denied", lambda svc, act: act == "delete")
    service = Mock()
    with pytest.raises(UserInputError, match="not allowed"):
        await _run(service, action="delete", reaction_name=REACTION)
    service.spaces.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"action": "create"}, "Invalid action"),
        ({"action": "list"}, "'message_name' is required"),
        ({"action": "delete"}, "'reaction_name' is required"),
        ({"action": "delete", "reaction_name": MSG}, "Invalid reaction_name"),
    ],
)
async def test_input_validation(kwargs, match):
    service = Mock()
    with pytest.raises(UserInputError, match=match):
        await _run(service, **kwargs)
    service.spaces.assert_not_called()
