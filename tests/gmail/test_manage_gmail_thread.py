"""Unit tests for manage_gmail_thread (Mock services only)."""

import os
import sys
from unittest.mock import Mock

import pytest
from fastmcp.exceptions import ToolError
from googleapiclient.errors import HttpError

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from auth.scopes import GMAIL_FULL_SCOPE, GMAIL_MODIFY_SCOPE
from core.utils import UserInputError
from gmail.gmail_management_tools import manage_gmail_thread

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
    service.users().threads().list().execute.return_value = {
        "threads": [
            {"id": "t1", "snippet": "First  thread   snippet", "historyId": "100"},
            {"id": "t2", "snippet": "", "historyId": "101"},
        ],
        "nextPageToken": "next-1",
        "resultSizeEstimate": 42,
    }
    for name in ("modify", "trash", "untrash"):
        getattr(service.users().threads(), name)().execute.return_value = {}
    service.users().threads().delete().execute.return_value = None
    # Configuring the chain above records calls; forget them.
    service.reset_mock()
    return service


async def _run(service, **kwargs):
    return await _unwrap(manage_gmail_thread)(
        service=service, user_google_email=EMAIL, **kwargs
    )


def test_requires_only_modify_scope():
    assert manage_gmail_thread._required_google_scopes == [GMAIL_MODIFY_SCOPE]


@pytest.mark.asyncio
async def test_list_forwards_filters_and_surfaces_page_token():
    service = _service()
    result = await _run(
        service,
        action="list",
        query="from:alice",
        label_ids=["INBOX", "Label_1"],
        max_results=10,
        page_token="p1",
        include_spam_trash=True,
    )
    service.users().threads().list.assert_called_with(
        userId="me",
        maxResults=10,
        includeSpamTrash=True,
        q="from:alice",
        labelIds=["INBOX", "Label_1"],
        pageToken="p1",
    )
    assert "Found 2 thread(s)" in result
    assert "estimated total: 42" in result
    assert "Thread ID: t1 | History ID: 100 | First thread snippet" in result
    assert "Thread ID: t2 | History ID: 101" in result
    assert "Next page token: next-1" in result


@pytest.mark.asyncio
async def test_list_defaults_and_empty_result():
    service = _service()
    service.users().threads().list().execute.return_value = {}
    result = await _run(service, action="list")
    service.users().threads().list.assert_called_with(
        userId="me", maxResults=20, includeSpamTrash=False
    )
    assert "No threads matched" in result


@pytest.mark.asyncio
async def test_list_caps_max_results():
    service = _service()
    await _run(service, action="list", max_results=5000)
    assert service.users().threads().list.call_args.kwargs["maxResults"] == 500


@pytest.mark.asyncio
async def test_modify_applies_labels_to_each_thread():
    service = _service()
    result = await _run(
        service,
        action="modify",
        thread_ids=["t1", "t2"],
        add_label_ids=["Label_1"],
        remove_label_ids=["INBOX"],
    )
    assert service.users().threads().modify.call_count == 2
    service.users().threads().modify.assert_any_call(
        userId="me",
        id="t2",
        body={"addLabelIds": ["Label_1"], "removeLabelIds": ["INBOX"]},
    )
    assert "Added labels: Label_1" in result
    assert "Removed labels: INBOX" in result


@pytest.mark.asyncio
async def test_modify_requires_some_label_change():
    service = _service()
    with pytest.raises(UserInputError, match="add_label_ids or remove_label_ids"):
        await _run(service, action="modify", thread_id="t1")
    service.users().threads().modify.assert_not_called()


@pytest.mark.asyncio
async def test_archive_removes_inbox():
    service = _service()
    result = await _run(service, action="archive", thread_id="t1")
    service.users().threads().modify.assert_called_with(
        userId="me", id="t1", body={"removeLabelIds": ["INBOX"]}
    )
    assert "Archived thread" in result


@pytest.mark.asyncio
async def test_trash_and_untrash():
    service = _service()
    result = await _run(service, action="trash", thread_ids=["t1", "t2"])
    assert service.users().threads().trash.call_count == 2
    assert "2 threads" in result

    service = _service()
    result = await _run(service, action="untrash", thread_id="t1")
    service.users().threads().untrash.assert_called_with(userId="me", id="t1")
    assert "Restored" in result


@pytest.mark.asyncio
async def test_action_without_ids_is_input_error():
    with pytest.raises(UserInputError, match="thread_id"):
        await _run(_service(), action="trash")


class TestPermanentDelete:
    @pytest.mark.asyncio
    async def test_refused_without_full_scope(self):
        service = _service(scopes=[GMAIL_MODIFY_SCOPE])
        with pytest.raises(ToolError, match="mail.google.com") as excinfo:
            await _run(service, action="delete", thread_id="t1")
        assert "Nothing was deleted" in str(excinfo.value)
        service.users().threads().delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_deletes_each_thread_with_full_scope(self):
        service = _service(scopes=[GMAIL_FULL_SCOPE])
        result = await _run(service, action="delete", thread_ids=["t1", "t2"])
        assert service.users().threads().delete.call_count == 2
        service.users().threads().delete.assert_any_call(userId="me", id="t1")
        assert "Permanently deleted 2 threads" in result

    @pytest.mark.asyncio
    async def test_403_is_explained_as_scope_problem(self):
        service = _service()
        service.users().threads().delete().execute.side_effect = HttpError(
            _FakeResp(403), b"{}"
        )
        with pytest.raises(ToolError, match="HTTP 403") as excinfo:
            await _run(service, action="delete", thread_id="t1")
        assert GMAIL_FULL_SCOPE in str(excinfo.value)
