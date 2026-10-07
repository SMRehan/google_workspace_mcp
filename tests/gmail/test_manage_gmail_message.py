"""Unit tests for manage_gmail_message (Mock services only)."""

import os
import sys
from unittest.mock import Mock

import pytest
from fastmcp.exceptions import ToolError
from googleapiclient.errors import HttpError

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from auth.permissions import set_permissions
from auth.scopes import GMAIL_FULL_SCOPE, GMAIL_MODIFY_SCOPE
from core.utils import UserInputError
from gmail.gmail_management_tools import manage_gmail_message

EMAIL = "user@example.com"


def _unwrap(tool):
    """Unwrap FunctionTool + decorators to the original async function."""
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


class _FakeResp:
    def __init__(self, status):
        self.status = status
        self.reason = "err"


def _service(scopes=None):
    service = Mock()
    if scopes is None:
        service._http = None
    else:
        service._http.credentials.scopes = list(scopes)
    service.users().messages().modify().execute.return_value = {}
    service.users().messages().batchModify().execute.return_value = {}
    service.users().messages().trash().execute.return_value = {}
    service.users().messages().untrash().execute.return_value = {}
    service.users().messages().delete().execute.return_value = None
    service.users().messages().batchDelete().execute.return_value = None
    # Configuring the chain above records calls; forget them.
    service.reset_mock()
    return service


async def _run(service, **kwargs):
    return await _unwrap(manage_gmail_message)(
        service=service, user_google_email=EMAIL, **kwargs
    )


@pytest.fixture(autouse=True)
def _reset_permissions():
    set_permissions(None)
    yield
    set_permissions(None)


def test_requires_only_modify_scope():
    assert manage_gmail_message._required_google_scopes == [GMAIL_MODIFY_SCOPE]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action, add, remove",
    [
        ("archive", None, ["INBOX"]),
        ("mark_read", None, ["UNREAD"]),
        ("mark_unread", ["UNREAD"], None),
        ("star", ["STARRED"], None),
        ("unstar", None, ["STARRED"]),
        ("mark_important", ["IMPORTANT"], None),
        ("mark_not_important", None, ["IMPORTANT"]),
    ],
)
async def test_label_actions_single_message_use_modify(action, add, remove):
    service = _service()
    result = await _run(service, action=action, message_id="m1")

    expected = {}
    if add:
        expected["addLabelIds"] = add
    if remove:
        expected["removeLabelIds"] = remove
    service.users().messages().modify.assert_called_with(
        userId="me", id="m1", body=expected
    )
    service.users().messages().batchModify.assert_not_called()
    assert action in result
    assert "m1" in result


@pytest.mark.asyncio
async def test_label_action_many_messages_uses_batch_modify():
    service = _service()
    result = await _run(service, action="archive", message_ids=["m1", "m2", "m1"])

    service.users().messages().batchModify.assert_called_with(
        userId="me", body={"ids": ["m1", "m2"], "removeLabelIds": ["INBOX"]}
    )
    service.users().messages().modify.assert_not_called()
    assert "2 messages" in result


@pytest.mark.asyncio
async def test_batch_modify_is_chunked_at_gmail_limit():
    service = _service()
    ids = [f"m{i}" for i in range(1001)]
    await _run(service, action="mark_read", message_ids=ids)

    calls = service.users().messages().batchModify.call_args_list
    assert len(calls) == 2
    assert len(calls[0].kwargs["body"]["ids"]) == 1000
    assert len(calls[1].kwargs["body"]["ids"]) == 1


@pytest.mark.asyncio
async def test_single_and_list_ids_are_merged():
    service = _service()
    await _run(service, action="star", message_id="m0", message_ids=["m1"])
    service.users().messages().batchModify.assert_called_with(
        userId="me", body={"ids": ["m0", "m1"], "addLabelIds": ["STARRED"]}
    )


@pytest.mark.asyncio
async def test_trash_and_untrash_use_dedicated_endpoints():
    service = _service()
    result = await _run(service, action="trash", message_ids=["m1", "m2"])
    assert service.users().messages().trash.call_count == 2
    service.users().messages().trash.assert_any_call(userId="me", id="m2")
    assert "Trash" in result

    service = _service()
    result = await _run(service, action="untrash", message_id="m1")
    service.users().messages().untrash.assert_called_with(userId="me", id="m1")
    assert "Restored" in result


@pytest.mark.asyncio
async def test_missing_ids_is_input_error():
    with pytest.raises(UserInputError, match="message_id"):
        await _run(_service(), action="archive")
    with pytest.raises(UserInputError, match="message_id"):
        await _run(_service(), action="archive", message_ids=["", "  "])


@pytest.mark.asyncio
async def test_invalid_action_is_input_error():
    with pytest.raises(UserInputError, match="Invalid action"):
        await _run(_service(), action="explode", message_id="m1")


class TestPermanentDelete:
    @pytest.mark.asyncio
    async def test_delete_refused_when_credentials_lack_full_scope(self):
        service = _service(scopes=[GMAIL_MODIFY_SCOPE])
        with pytest.raises(ToolError, match="mail.google.com") as excinfo:
            await _run(service, action="delete", message_id="m1")
        assert "Nothing was deleted" in str(excinfo.value)
        assert "trash" in str(excinfo.value)
        service.users().messages().delete.assert_not_called()
        service.users().messages().batchDelete.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_single_with_full_scope(self):
        service = _service(scopes=[GMAIL_FULL_SCOPE])
        result = await _run(service, action="delete", message_id="m1")
        service.users().messages().delete.assert_called_with(userId="me", id="m1")
        assert "Permanently deleted" in result
        assert "cannot be undone" in result

    @pytest.mark.asyncio
    async def test_delete_many_uses_batch_delete(self):
        service = _service(scopes=[GMAIL_FULL_SCOPE])
        await _run(service, action="delete", message_ids=["m1", "m2"])
        service.users().messages().batchDelete.assert_called_with(
            userId="me", body={"ids": ["m1", "m2"]}
        )
        service.users().messages().delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_403_from_api_is_explained_as_scope_problem(self):
        # Scopes unknown on the credentials: the API itself says no.
        service = _service()
        service.users().messages().delete().execute.side_effect = HttpError(
            _FakeResp(403), b'{"error": {"message": "Insufficient Permission"}}'
        )
        with pytest.raises(ToolError, match="HTTP 403") as excinfo:
            await _run(service, action="delete", message_id="m1")
        assert GMAIL_FULL_SCOPE in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_delete_other_http_errors_propagate(self):
        service = _service()
        service.users().messages().delete().execute.side_effect = HttpError(
            _FakeResp(404), b"{}"
        )
        with pytest.raises(HttpError):
            await _run(service, action="delete", message_id="m1")


@pytest.mark.asyncio
async def test_permission_denied_action_is_rejected_before_any_call(monkeypatch):
    from auth import permissions

    monkeypatch.setitem(
        permissions.SERVICE_DENIED_ACTIONS, "gmail", {"organize": frozenset({"trash"})}
    )
    set_permissions({"gmail": "organize"})
    service = _service()
    with pytest.raises(UserInputError, match="not allowed"):
        await _run(service, action="trash", message_id="m1")
    service.users().messages().trash.assert_not_called()
