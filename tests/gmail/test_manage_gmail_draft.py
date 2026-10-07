"""Unit tests for manage_gmail_draft (Mock services only)."""

import base64
import email
import os
import sys
from email import policy
from unittest.mock import Mock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from auth.scopes import GMAIL_COMPOSE_SCOPE
from core.utils import UserInputError
from gmail.gmail_management_tools import manage_gmail_draft

EMAIL = "user@example.com"


def _unwrap(tool):
    """Unwrap FunctionTool + decorators to the original async function."""
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode()


def _headers(**values):
    return [{"name": k.replace("_", "-"), "value": v} for k, v in values.items()]


def _draft(draft_id="d1", text="Hello there", html=None, attachments=False):
    parts = [{"mimeType": "text/plain", "body": {"data": _b64(text)}}]
    if html is not None:
        parts.append({"mimeType": "text/html", "body": {"data": _b64(html)}})
    if attachments:
        parts.append(
            {
                "mimeType": "application/pdf",
                "filename": "report.pdf",
                "body": {"attachmentId": "att1", "size": 10},
            }
        )
    return {
        "id": draft_id,
        "message": {
            "id": "msg1",
            "threadId": "thr1",
            "payload": {
                "mimeType": "multipart/mixed",
                "headers": _headers(
                    From="Me <user@example.com>",
                    To="alice@example.com",
                    Cc="bob@example.com",
                    Subject="Weekly report",
                    Date="Mon, 1 Jan 2026 10:00:00 +0000",
                    In_Reply_To="<orig@example.com>",
                    References="<orig@example.com>",
                ),
                "parts": parts,
            },
        },
    }


def _service():
    service = Mock()
    service.users().drafts().list().execute.return_value = {
        "drafts": [{"id": "d1", "message": {"id": "msg1", "threadId": "thr1"}}],
        "nextPageToken": "tok2",
        "resultSizeEstimate": 1,
    }
    service.users().drafts().get().execute.return_value = _draft()
    service.users().drafts().update().execute.return_value = {"id": "d1"}
    service.users().drafts().send().execute.return_value = {
        "id": "sent1",
        "threadId": "thr1",
    }
    service.users().drafts().delete().execute.return_value = None
    # Configuring the chain above records calls; forget them.
    service.reset_mock()
    return service


async def _run(service, **kwargs):
    return await _unwrap(manage_gmail_draft)(
        service=service, user_google_email=EMAIL, **kwargs
    )


def _decode_raw(service):
    body = service.users().drafts().update.call_args.kwargs["body"]
    raw = base64.urlsafe_b64decode(body["message"]["raw"])
    return body, email.message_from_bytes(raw, policy=policy.default)


def test_requires_only_compose_scope():
    assert manage_gmail_draft._required_google_scopes == [GMAIL_COMPOSE_SCOPE]


@pytest.mark.asyncio
async def test_list_forwards_query_and_paging_and_shows_metadata():
    service = _service()
    result = await _run(
        service, action="list", query="subject:report", max_results=5, page_token="tok1"
    )

    service.users().drafts().list.assert_called_with(
        userId="me", maxResults=5, q="subject:report", pageToken="tok1"
    )
    service.users().drafts().get.assert_called_with(
        userId="me", id="d1", format="metadata"
    )
    assert "Draft ID: d1" in result
    assert "Subject: Weekly report" in result
    assert "To: alice@example.com" in result
    assert "Next page token: tok2" in result


@pytest.mark.asyncio
async def test_list_empty():
    service = _service()
    service.users().drafts().list().execute.return_value = {}
    result = await _run(service, action="list")
    assert "No drafts found" in result
    service.users().drafts().get.assert_not_called()


@pytest.mark.asyncio
async def test_list_rejects_bad_max_results():
    with pytest.raises(UserInputError, match="max_results"):
        await _run(_service(), action="list", max_results=0)


@pytest.mark.asyncio
async def test_get_returns_headers_body_and_attachments():
    service = _service()
    service.users().drafts().get().execute.return_value = _draft(attachments=True)
    result = await _run(service, action="get", draft_id="d1")

    service.users().drafts().get.assert_called_with(userId="me", id="d1", format="full")
    for line in (
        "Draft ID: d1",
        "Thread ID: thr1",
        "From: Me <user@example.com>",
        "To: alice@example.com",
        "Cc: bob@example.com",
        "Bcc: ",
        "Subject: Weekly report",
        "In-Reply-To: <orig@example.com>",
        "Attachments: report.pdf",
        "--- BODY (text) ---",
        "Hello there",
    ):
        assert line in result


@pytest.mark.asyncio
async def test_get_falls_back_to_html_body():
    service = _service()
    service.users().drafts().get().execute.return_value = _draft(
        text="", html="<p>Hi</p>"
    )
    result = await _run(service, action="get", draft_id="d1")
    assert "--- BODY (html) ---" in result
    assert "<p>Hi</p>" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["get", "update", "send", "delete"])
async def test_actions_require_draft_id(action):
    with pytest.raises(UserInputError, match="draft_id"):
        await _run(_service(), action=action, subject="x")


@pytest.mark.asyncio
async def test_update_replaces_given_fields_and_keeps_the_rest():
    service = _service()
    result = await _run(
        service,
        action="update",
        draft_id="d1",
        subject="New subject",
        to="carol@example.com",
    )

    body, message = _decode_raw(service)
    assert body["id"] == "d1"
    assert body["message"]["threadId"] == "thr1"
    assert message["Subject"] == "New subject"
    assert message["To"] == "carol@example.com"
    # Untouched fields are carried over from the current draft.
    assert message["Cc"] == "bob@example.com"
    assert message["From"] == "Me <user@example.com>"
    assert message["In-Reply-To"] == "<orig@example.com>"
    assert message["References"] == "<orig@example.com>"
    assert message.get_content().strip() == "Hello there"
    assert "Changed: subject, to" in result
    service.users().drafts().update.assert_called_with(userId="me", id="d1", body=body)


@pytest.mark.asyncio
async def test_update_body_html_and_clearing_cc():
    service = _service()
    await _run(
        service,
        action="update",
        draft_id="d1",
        body="<b>Bold</b>",
        body_format="html",
        cc="",
    )
    _, message = _decode_raw(service)
    assert message.get_content_type() == "text/html"
    assert "<b>Bold</b>" in message.get_content()
    assert message["Cc"] is None
    assert message["To"] == "alice@example.com"


@pytest.mark.asyncio
async def test_update_keeps_html_body_when_draft_has_only_html():
    service = _service()
    service.users().drafts().get().execute.return_value = _draft(
        text="", html="<i>x</i>"
    )
    await _run(service, action="update", draft_id="d1", subject="S")
    _, message = _decode_raw(service)
    assert message.get_content_type() == "text/html"
    assert "<i>x</i>" in message.get_content()


@pytest.mark.asyncio
async def test_update_warns_when_attachments_are_dropped():
    service = _service()
    service.users().drafts().get().execute.return_value = _draft(attachments=True)
    result = await _run(service, action="update", draft_id="d1", subject="S")
    assert "attachments" in result.lower()
    assert "not carried over" in result


@pytest.mark.asyncio
async def test_update_without_changes_is_input_error():
    service = _service()
    with pytest.raises(UserInputError, match="at least one"):
        await _run(service, action="update", draft_id="d1")
    service.users().drafts().update.assert_not_called()


@pytest.mark.asyncio
async def test_send_uses_drafts_send_and_reports_message_id():
    service = _service()
    result = await _run(service, action="send", draft_id="d1")
    service.users().drafts().send.assert_called_with(userId="me", body={"id": "d1"})
    assert "Message ID: sent1" in result
    assert "Thread ID: thr1" in result


@pytest.mark.asyncio
async def test_delete_calls_drafts_delete():
    service = _service()
    result = await _run(service, action="delete", draft_id=" d1 ")
    service.users().drafts().delete.assert_called_with(userId="me", id="d1")
    assert "deleted" in result


@pytest.mark.asyncio
async def test_invalid_action():
    with pytest.raises(UserInputError, match="Invalid action"):
        await _run(_service(), action="create", draft_id="d1")
