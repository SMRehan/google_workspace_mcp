"""Unit tests for manage_chat_message (spaces.messages get/patch/delete)."""

import os
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from core.utils import UserInputError
from gchat.chat_tools import manage_chat_message

EMAIL = "me@example.com"
MSG = "spaces/S/messages/M"


def _unwrap(tool):
    fn = getattr(tool, "fn", tool)
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


async def _run(service, **kwargs):
    return await _unwrap(manage_chat_message)(
        service=service, user_google_email=EMAIL, **kwargs
    )


@pytest.mark.asyncio
async def test_get_message_formats_details():
    service = Mock()
    service.spaces().messages().get().execute.return_value = {
        "name": MSG,
        "text": "hello",
        "createTime": "2026-01-01T00:00:00Z",
        "lastUpdateTime": "2026-01-02T00:00:00Z",
        "sender": {"name": "users/1", "displayName": "Ann"},
        "thread": {"name": "spaces/S/threads/T"},
        "attachment": [{"contentName": "a.png", "contentType": "image/png"}],
        "annotations": [
            {
                "type": "RICH_LINK",
                "richLinkMetadata": {"uri": "https://docs.google.com/x"},
            }
        ],
        "emojiReactionSummaries": [{"emoji": {"unicode": "👍"}, "reactionCount": 2}],
    }
    result = await _run(service, action="get", message_name=MSG)
    assert f"Message {MSG}" in result
    assert "- From: Ann" in result
    assert "- Last updated: 2026-01-02T00:00:00Z" in result
    assert "- Thread: spaces/S/threads/T" in result
    assert "- Text: hello" in result
    assert "- Linked: https://docs.google.com/x" in result
    assert "- Attachment 0: a.png (image/png)" in result
    assert "- Reactions: 👍x2" in result
    service.spaces().messages().get.assert_called_with(name=MSG)


@pytest.mark.asyncio
async def test_update_message_defaults_mask_to_text():
    service = Mock()
    service.spaces().messages().patch().execute.return_value = {
        "name": MSG,
        "lastUpdateTime": "2026-01-02T00:00:00Z",
    }
    result = await _run(service, action="update", message_name=MSG, text="fixed")
    assert "Message updated (fields: text)" in result
    assert "2026-01-02T00:00:00Z" in result
    service.spaces().messages().patch.assert_called_with(
        name=MSG, updateMask="text", body={"text": "fixed"}
    )


@pytest.mark.asyncio
async def test_update_message_custom_mask_without_text():
    service = Mock()
    service.spaces().messages().patch().execute.return_value = {"name": MSG}
    await _run(
        service,
        action="update",
        message_name=MSG,
        update_mask="quoted_message_metadata",
    )
    service.spaces().messages().patch.assert_called_with(
        name=MSG, updateMask="quoted_message_metadata", body={}
    )


@pytest.mark.asyncio
async def test_update_message_rejects_app_only_fields():
    service = Mock()
    with pytest.raises(UserInputError, match="cards_v2"):
        await _run(
            service,
            action="update",
            message_name=MSG,
            text="x",
            update_mask="text, cards_v2",
        )
    service.spaces.assert_not_called()


@pytest.mark.asyncio
async def test_update_message_requires_text_when_mask_has_text():
    service = Mock()
    with pytest.raises(UserInputError, match="'text' is required"):
        await _run(service, action="update", message_name=MSG)
    service.spaces.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("force", [False, True])
async def test_delete_message_forwards_force(force):
    service = Mock()
    result = await _run(service, action="delete", message_name=MSG, force=force)
    assert result.startswith(f"Deleted message {MSG}")
    assert ("threaded replies" in result) is force
    service.spaces().messages().delete.assert_called_with(name=MSG, force=force)


@pytest.mark.asyncio
async def test_delete_denied_under_permission_level(monkeypatch):
    import gchat.chat_helpers as helpers

    monkeypatch.setattr(helpers, "is_action_denied", lambda svc, act: act == "delete")
    service = Mock()
    with pytest.raises(UserInputError, match="not allowed"):
        await _run(service, action="delete", message_name=MSG)
    service.spaces.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"action": "send", "message_name": MSG}, "Invalid action"),
        ({"action": "get", "message_name": ""}, "'message_name' is required"),
        (
            {"action": "get", "message_name": "spaces/S/threads/T"},
            "Invalid message_name",
        ),
    ],
)
async def test_input_validation(kwargs, match):
    service = Mock()
    with pytest.raises(UserInputError, match=match):
        await _run(service, **kwargs)
    service.spaces.assert_not_called()
