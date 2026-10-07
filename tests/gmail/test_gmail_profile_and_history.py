"""Unit tests for get_gmail_profile and list_gmail_history (Mock services only)."""

import os
import sys
from unittest.mock import Mock

import pytest
from googleapiclient.errors import HttpError

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from auth.scopes import GMAIL_READONLY_SCOPE
from core.utils import UserInputError
from gmail.gmail_management_tools import get_gmail_profile, list_gmail_history

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


def test_tools_need_only_readonly_scope():
    assert get_gmail_profile._required_google_scopes == [GMAIL_READONLY_SCOPE]
    assert list_gmail_history._required_google_scopes == [GMAIL_READONLY_SCOPE]


@pytest.mark.asyncio
async def test_get_profile_formats_fields():
    service = Mock()
    service.users().getProfile().execute.return_value = {
        "emailAddress": EMAIL,
        "messagesTotal": 1234,
        "threadsTotal": 567,
        "historyId": "98765",
    }
    result = await _unwrap(get_gmail_profile)(service=service, user_google_email=EMAIL)
    service.users().getProfile.assert_called_with(userId="me")
    assert result == (
        f"Email: {EMAIL}\nMessages total: 1234\nThreads total: 567\nHistory ID: 98765"
    )


def _history_service(response):
    service = Mock()
    service.users().history().list().execute.return_value = response
    return service


async def _history(service, **kwargs):
    return await _unwrap(list_gmail_history)(
        service=service, user_google_email=EMAIL, **kwargs
    )


@pytest.mark.asyncio
async def test_history_forwards_parameters_and_summarizes():
    service = _history_service(
        {
            "historyId": "200",
            "nextPageToken": "hp2",
            "history": [
                {
                    "id": "101",
                    "messagesAdded": [
                        {
                            "message": {
                                "id": "m1",
                                "threadId": "t1",
                                "labelIds": ["INBOX", "UNREAD"],
                            }
                        }
                    ],
                    "labelsAdded": [
                        {
                            "message": {"id": "m2", "threadId": "t2"},
                            "labelIds": ["STARRED"],
                        }
                    ],
                },
                {
                    "id": "102",
                    "messagesDeleted": [{"message": {"id": "m3", "threadId": "t3"}}],
                    "labelsRemoved": [
                        {
                            "message": {"id": "m1", "threadId": "t1"},
                            "labelIds": ["UNREAD"],
                        }
                    ],
                },
            ],
        }
    )
    result = await _history(
        service,
        start_history_id="100",
        history_types=["messageAdded", "labelRemoved"],
        label_id="INBOX",
        max_results=50,
        page_token="hp1",
    )
    service.users().history().list.assert_called_with(
        userId="me",
        startHistoryId="100",
        maxResults=50,
        historyTypes=["messageAdded", "labelRemoved"],
        labelId="INBOX",
        pageToken="hp1",
    )
    assert "History since 100: 2 record(s)" in result
    assert (
        "Messages added: 1 | Messages deleted: 1 | Labels added: 1 | Labels removed: 1"
        in result
    )
    assert "Current history ID: 200" in result
    assert "[101] message m1 (thread t1) labels: INBOX, UNREAD" in result
    assert "[101] message m2 +STARRED" in result
    assert "[102] message m3 (thread t3)" in result
    assert "[102] message m1 -UNREAD" in result
    assert "Next page token: hp2" in result


@pytest.mark.asyncio
async def test_history_empty_page():
    service = _history_service({"historyId": "150"})
    result = await _history(service, start_history_id="100")
    service.users().history().list.assert_called_with(
        userId="me", startHistoryId="100", maxResults=100
    )
    assert "No changes recorded" in result
    assert "Current history ID: 150" in result


@pytest.mark.asyncio
async def test_history_rejects_non_numeric_start():
    with pytest.raises(UserInputError, match="numeric"):
        await _history(_history_service({}), start_history_id="abc")


@pytest.mark.asyncio
async def test_history_rejects_unknown_type():
    with pytest.raises(UserInputError, match="history_types"):
        await _history(
            _history_service({}), start_history_id="1", history_types=["messageMoved"]
        )


@pytest.mark.asyncio
async def test_history_404_becomes_clear_input_error():
    service = _history_service({})
    service.users().history().list().execute.side_effect = HttpError(
        _FakeResp(404), b"{}"
    )
    with pytest.raises(UserInputError, match="too old") as excinfo:
        await _history(service, start_history_id="5")
    assert "get_gmail_profile" in str(excinfo.value)


@pytest.mark.asyncio
async def test_history_other_errors_propagate():
    service = _history_service({})
    service.users().history().list().execute.side_effect = HttpError(
        _FakeResp(500), b"{}"
    )
    with pytest.raises(HttpError):
        await _history(service, start_history_id="5")
