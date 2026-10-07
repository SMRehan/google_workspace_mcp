"""Unit tests for the four Keep tools, driven with Mock services."""

import base64
import os
import sys
from unittest.mock import AsyncMock, Mock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from core.file_limits import FileTooLargeError
from core.utils import UserInputError
from gkeep.keep_tools import (
    get_keep_note,
    list_keep_notes,
    manage_keep_note,
    manage_keep_note_permissions,
)

EMAIL = "user@example.com"


def _unwrap(tool):
    """Unwrap FunctionTool + decorators to the original async function."""
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _note(name="notes/n1", title="Groceries", **overrides):
    note = {
        "name": name,
        "title": title,
        "body": {"text": {"text": "eggs"}},
        "updateTime": "2026-05-01T10:00:00Z",
        "createTime": "2026-04-30T10:00:00Z",
        "permissions": [
            {"name": f"{name}/permissions/owner", "email": EMAIL, "role": "OWNER"}
        ],
    }
    note.update(overrides)
    return note


def _service(list_response=None, get_response=None):
    service = Mock()
    service.notes().list().execute = Mock(return_value=list_response or {})
    service.notes().get().execute = Mock(return_value=get_response or {})
    return service


class TestListKeepNotes:
    @pytest.mark.asyncio
    async def test_compact_listing(self):
        service = _service(
            list_response={
                "notes": [
                    _note(),
                    _note(
                        "notes/n2",
                        "Trip",
                        body={
                            "list": {
                                "listItems": [
                                    {"text": {"text": "a"}, "checked": True},
                                    {
                                        "text": {"text": "b"},
                                        "childListItems": [{"text": {"text": "c"}}],
                                    },
                                ]
                            }
                        },
                        attachments=[{"name": "notes/n2/attachments/x"}],
                    ),
                ]
            }
        )
        result = await _unwrap(list_keep_notes)(
            service=service, user_google_email=EMAIL
        )

        assert f"Keep notes for {EMAIL}:" in result
        assert "- Groceries (ID: notes/n1)" in result
        assert "Kind: text (4 chars)" in result
        assert "- Trip (ID: notes/n2)" in result
        assert "Kind: list (1/3 checked) | Attachments: 1" in result
        assert "Updated: 2026-05-01T10:00:00Z" in result
        assert "Next page token" not in result

    @pytest.mark.asyncio
    async def test_filter_and_paging_are_forwarded(self):
        service = _service(list_response={"notes": [_note()], "nextPageToken": "tok2"})

        result = await _unwrap(list_keep_notes)(
            service=service,
            user_google_email=EMAIL,
            filter="trashed=false",
            page_size=5,
            page_token="tok1",
        )

        assert service.notes().list.call_args.kwargs == {
            "pageSize": 5,
            "filter": "trashed=false",
            "pageToken": "tok1",
        }
        assert "(filter: trashed=false)" in result
        assert "Next page token: tok2" in result

    @pytest.mark.asyncio
    async def test_defaults_omit_filter_and_token(self):
        service = _service(list_response={"notes": []})

        result = await _unwrap(list_keep_notes)(
            service=service, user_google_email=EMAIL
        )

        assert service.notes().list.call_args.kwargs == {"pageSize": 20}
        assert result == f"No Keep notes found for {EMAIL}."

    @pytest.mark.asyncio
    @pytest.mark.parametrize("page_size", [0, 101])
    async def test_rejects_bad_page_size(self, page_size):
        with pytest.raises(UserInputError, match="page_size"):
            await _unwrap(list_keep_notes)(
                service=_service(), user_google_email=EMAIL, page_size=page_size
            )


class TestGetKeepNote:
    @pytest.mark.asyncio
    async def test_renders_text_note_and_normalizes_id(self):
        service = _service(get_response=_note())

        result = await _unwrap(get_keep_note)(
            service=service, user_google_email=EMAIL, note_id="n1"
        )

        assert service.notes().get.call_args.kwargs == {"name": "notes/n1"}
        assert "Title: Groceries" in result
        assert "Text:\neggs" in result
        assert f"- {EMAIL}: OWNER" in result

    @pytest.mark.asyncio
    async def test_renders_checklist_with_nested_items(self):
        service = _service(
            get_response=_note(
                body={
                    "list": {
                        "listItems": [
                            {
                                "text": {"text": "Pack"},
                                "checked": False,
                                "childListItems": [
                                    {"text": {"text": "Charger"}, "checked": True}
                                ],
                            }
                        ]
                    }
                }
            )
        )

        result = await _unwrap(get_keep_note)(
            service=service, user_google_email=EMAIL, note_id="notes/n1"
        )

        assert "Checklist (1/2 checked):" in result
        assert "[ ] Pack\n  [x] Charger" in result

    @pytest.mark.asyncio
    async def test_lists_attachments(self):
        service = _service(
            get_response=_note(
                attachments=[
                    {"name": "notes/n1/attachments/a1", "mimeType": ["image/png"]}
                ]
            )
        )

        result = await _unwrap(get_keep_note)(
            service=service, user_google_email=EMAIL, note_id="n1"
        )

        assert "Attachments (1):" in result
        assert "- notes/n1/attachments/a1 [image/png]" in result

    @pytest.mark.asyncio
    async def test_downloads_text_attachment_inline(self):
        service = _service(
            get_response=_note(
                attachments=[
                    {"name": "notes/n1/attachments/a1", "mimeType": ["text/plain"]}
                ]
            )
        )
        download = AsyncMock(return_value=b"hello keep")
        with patch("gkeep.keep_helpers.download_media_bytes", download):
            result = await _unwrap(get_keep_note)(
                service=service,
                user_google_email=EMAIL,
                note_id="n1",
                download_attachment="a1",
            )

        assert service.media().download_media.call_args.kwargs == {
            "name": "notes/n1/attachments/a1",
            "mimeType": "text/plain",
        }
        request_obj = download.call_args.args[0]
        assert request_obj is service.media().download_media.return_value
        assert download.call_args.kwargs["kind"] == "attachment"
        assert "MIME type: text/plain" in result
        assert "Size: 10 bytes" in result
        assert "hello keep" in result

    @pytest.mark.asyncio
    async def test_binary_attachment_reports_size_without_base64_by_default(self):
        service = _service(
            get_response=_note(
                attachments=[
                    {
                        "name": "notes/n1/attachments/a1",
                        "mimeType": ["image/png", "image/jpeg"],
                    }
                ]
            )
        )
        payload = b"\x89PNG\r\n"
        with patch(
            "gkeep.keep_helpers.download_media_bytes", AsyncMock(return_value=payload)
        ):
            result = await _unwrap(get_keep_note)(
                service=service,
                user_google_email=EMAIL,
                note_id="n1",
                download_attachment="notes/n1/attachments/a1",
                mime_type="image/png",
            )

        assert "Size: 6 bytes" in result
        assert "include_base64=True" in result
        assert base64.b64encode(payload).decode() not in result

    @pytest.mark.asyncio
    async def test_binary_attachment_with_base64(self):
        service = _service(
            get_response=_note(
                attachments=[
                    {"name": "notes/n1/attachments/a1", "mimeType": ["image/png"]}
                ]
            )
        )
        payload = b"\x89PNG\r\n"
        with patch(
            "gkeep.keep_helpers.download_media_bytes", AsyncMock(return_value=payload)
        ):
            result = await _unwrap(get_keep_note)(
                service=service,
                user_google_email=EMAIL,
                note_id="n1",
                download_attachment="a1",
                include_base64=True,
            )

        assert "Base64 content:" in result
        assert base64.b64encode(payload).decode() in result

    @pytest.mark.asyncio
    async def test_attachment_mime_type_mismatch_is_input_error(self):
        service = _service(
            get_response=_note(
                attachments=[
                    {"name": "notes/n1/attachments/a1", "mimeType": ["image/png"]}
                ]
            )
        )
        with pytest.raises(UserInputError, match="not available as"):
            await _unwrap(get_keep_note)(
                service=service,
                user_google_email=EMAIL,
                note_id="n1",
                download_attachment="a1",
                mime_type="image/gif",
            )
        service.media().download_media.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_attachment_is_input_error(self):
        service = _service(get_response=_note(attachments=[]))
        with pytest.raises(UserInputError, match="not found on note"):
            await _unwrap(get_keep_note)(
                service=service,
                user_google_email=EMAIL,
                note_id="n1",
                download_attachment="missing",
            )

    @pytest.mark.asyncio
    async def test_too_large_attachment_returns_limit_message(self):
        service = _service(
            get_response=_note(
                attachments=[
                    {"name": "notes/n1/attachments/a1", "mimeType": ["image/png"]}
                ]
            )
        )
        with patch(
            "gkeep.keep_helpers.download_media_bytes",
            AsyncMock(side_effect=FileTooLargeError("Error: too large")),
        ):
            result = await _unwrap(get_keep_note)(
                service=service,
                user_google_email=EMAIL,
                note_id="n1",
                download_attachment="a1",
            )
        assert result == "Error: too large"


class TestManageKeepNote:
    @pytest.mark.asyncio
    async def test_create_text_note(self):
        service = Mock()
        service.notes().create().execute = Mock(
            return_value={"name": "notes/new", "title": "Hi"}
        )

        result = await _unwrap(manage_keep_note)(
            service=service,
            user_google_email=EMAIL,
            action="create",
            title="Hi",
            text="body",
        )

        assert service.notes().create.call_args.kwargs == {
            "body": {"title": "Hi", "body": {"text": {"text": "body"}}}
        }
        assert "Created Keep note 'Hi' (ID: notes/new)" in result

    @pytest.mark.asyncio
    async def test_create_checklist_note(self):
        service = Mock()
        service.notes().create().execute = Mock(return_value={"name": "notes/new"})

        await _unwrap(manage_keep_note)(
            service=service,
            user_google_email=EMAIL,
            action="create",
            list_items=[
                {"text": "A", "checked": True, "children": [{"text": "A1"}]},
                {"text": "B"},
            ],
        )

        body = service.notes().create.call_args.kwargs["body"]
        assert "title" not in body
        assert body["body"]["list"]["listItems"] == [
            {
                "text": {"text": "A"},
                "checked": True,
                "childListItems": [{"text": {"text": "A1"}, "checked": False}],
            },
            {"text": {"text": "B"}, "checked": False},
        ]

    @pytest.mark.asyncio
    async def test_create_requires_a_body(self):
        with pytest.raises(UserInputError, match="needs a body"):
            await _unwrap(manage_keep_note)(
                service=Mock(), user_google_email=EMAIL, action="create", title="x"
            )

    @pytest.mark.asyncio
    async def test_delete_note(self):
        service = Mock()
        service.notes().delete().execute = Mock(return_value={})

        result = await _unwrap(manage_keep_note)(
            service=service, user_google_email=EMAIL, action="delete", note_id="n1"
        )

        assert service.notes().delete.call_args.kwargs == {"name": "notes/n1"}
        assert result == f"Deleted Keep note notes/n1 for {EMAIL}."

    @pytest.mark.asyncio
    async def test_delete_requires_note_id(self):
        with pytest.raises(UserInputError, match="note_id"):
            await _unwrap(manage_keep_note)(
                service=Mock(), user_google_email=EMAIL, action="delete"
            )

    @pytest.mark.asyncio
    async def test_invalid_action(self):
        with pytest.raises(UserInputError, match="Invalid action"):
            await _unwrap(manage_keep_note)(
                service=Mock(), user_google_email=EMAIL, action="update"
            )

    @pytest.mark.asyncio
    async def test_delete_denied_by_permission_level(self):
        service = Mock()
        with patch("gkeep.keep_tools.is_action_denied", return_value=True) as denied:
            with pytest.raises(UserInputError, match="not allowed"):
                await _unwrap(manage_keep_note)(
                    service=service,
                    user_google_email=EMAIL,
                    action="delete",
                    note_id="n1",
                )
        denied.assert_called_once_with("keep", "delete")
        service.notes().delete.assert_not_called()


class TestManageKeepNotePermissions:
    @pytest.mark.asyncio
    async def test_list_permissions(self):
        service = _service(
            get_response=_note(
                permissions=[
                    {"name": "notes/n1/permissions/o", "email": EMAIL, "role": "OWNER"},
                    {
                        "name": "notes/n1/permissions/w",
                        "email": "w@x.com",
                        "role": "WRITER",
                    },
                ]
            )
        )

        result = await _unwrap(manage_keep_note_permissions)(
            service=service, user_google_email=EMAIL, action="list", note_id="n1"
        )

        assert service.notes().get.call_args.kwargs == {"name": "notes/n1"}
        assert "Permissions on notes/n1:" in result
        assert f"- {EMAIL}: OWNER (ID: notes/n1/permissions/o)" in result
        assert "- w@x.com: WRITER (ID: notes/n1/permissions/w)" in result

    @pytest.mark.asyncio
    async def test_grant_uses_batch_create_with_writer_role(self):
        service = Mock()
        service.notes().permissions().batchCreate().execute = Mock(
            return_value={
                "permissions": [
                    {
                        "name": "notes/n1/permissions/p9",
                        "email": "w@x.com",
                        "role": "WRITER",
                    }
                ]
            }
        )

        result = await _unwrap(manage_keep_note_permissions)(
            service=service,
            user_google_email=EMAIL,
            action="grant",
            note_id="n1",
            emails=["w@x.com"],
        )

        assert service.notes().permissions().batchCreate.call_args.kwargs == {
            "parent": "notes/n1",
            "body": {
                "requests": [
                    {
                        "parent": "notes/n1",
                        "permission": {"email": "w@x.com", "role": "WRITER"},
                    }
                ]
            },
        }
        assert "Granted WRITER access on notes/n1 to 1 principal(s)" in result
        assert "- w@x.com: WRITER (ID: notes/n1/permissions/p9)" in result

    @pytest.mark.asyncio
    async def test_grant_requires_emails(self):
        with pytest.raises(UserInputError, match="emails"):
            await _unwrap(manage_keep_note_permissions)(
                service=Mock(), user_google_email=EMAIL, action="grant", note_id="n1"
            )

    @pytest.mark.asyncio
    async def test_revoke_uses_batch_delete_with_full_names(self):
        service = Mock()
        service.notes().permissions().batchDelete().execute = Mock(return_value={})

        result = await _unwrap(manage_keep_note_permissions)(
            service=service,
            user_google_email=EMAIL,
            action="revoke",
            note_id="notes/n1",
            permission_names=["p1", "notes/n1/permissions/p2"],
        )

        assert service.notes().permissions().batchDelete.call_args.kwargs == {
            "parent": "notes/n1",
            "body": {"names": ["notes/n1/permissions/p1", "notes/n1/permissions/p2"]},
        }
        assert "Revoked 2 permission(s) on notes/n1" in result
        assert "- notes/n1/permissions/p2" in result

    @pytest.mark.asyncio
    async def test_revoke_requires_permission_names(self):
        with pytest.raises(UserInputError, match="permission_names"):
            await _unwrap(manage_keep_note_permissions)(
                service=Mock(), user_google_email=EMAIL, action="revoke", note_id="n1"
            )

    @pytest.mark.asyncio
    async def test_invalid_action(self):
        with pytest.raises(UserInputError, match="Invalid action"):
            await _unwrap(manage_keep_note_permissions)(
                service=Mock(), user_google_email=EMAIL, action="share", note_id="n1"
            )

    @pytest.mark.asyncio
    async def test_denied_by_permission_level(self):
        service = Mock()
        with patch("gkeep.keep_tools.is_action_denied", return_value=True):
            with pytest.raises(UserInputError, match="not allowed"):
                await _unwrap(manage_keep_note_permissions)(
                    service=service,
                    user_google_email=EMAIL,
                    action="grant",
                    note_id="n1",
                    emails=["w@x.com"],
                )
        service.notes().permissions().batchCreate.assert_not_called()
