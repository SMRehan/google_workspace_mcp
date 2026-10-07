"""Unit tests for manage_drive_revisions."""

import base64
import os
import sys
from unittest.mock import AsyncMock, Mock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from core.utils import UserInputError
from gdrive.drive_helpers import (
    REVISION_ACTIONS,
    REVISION_FIELDS,
    _manage_drive_revisions_impl,
    _render_revision_content,
)
from gdrive.drive_tools import manage_drive_revisions

EMAIL = "user@example.com"


def _unwrap(tool):
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _revision(rev_id="r1", **extra):
    base = {
        "id": rev_id,
        "modifiedTime": "2026-01-02T03:04:05Z",
        "mimeType": "application/pdf",
        "size": "2048",
        "keepForever": False,
        "lastModifyingUser": {"displayName": "Ada", "emailAddress": EMAIL},
    }
    base.update(extra)
    return base


def _service(list_response=None, get_response=None, update_response=None):
    service = Mock()
    revisions = service.revisions.return_value
    revisions.list.return_value.execute.return_value = list_response or {}
    revisions.get.return_value.execute.return_value = get_response or _revision()
    revisions.update.return_value.execute.return_value = update_response or _revision()
    revisions.delete.return_value.execute.return_value = ""
    return service


class TestList:
    @pytest.mark.asyncio
    async def test_list_formats_each_revision(self):
        service = _service(
            {
                "revisions": [
                    _revision("r1"),
                    _revision("r2", keepForever=True, size="10"),
                ],
                "nextPageToken": "tok2",
            }
        )

        result = await _manage_drive_revisions_impl(service, EMAIL, "f1", "list")

        kwargs = service.revisions.return_value.list.call_args.kwargs
        assert kwargs["fileId"] == "f1"
        assert kwargs["pageSize"] == 100
        assert "pageToken" not in kwargs
        assert kwargs["fields"] == f"nextPageToken, revisions({REVISION_FIELDS})"
        assert "Found 2 revisions for file f1" in result
        assert "Revision ID: r1" in result
        assert "Modified: 2026-01-02T03:04:05Z by Ada <user@example.com>" in result
        assert "Size: 2.0 KB (2,048 bytes)" in result
        assert "Keep forever: yes" in result
        assert "Keep forever: no" in result
        assert result.endswith("nextPageToken: tok2")

    @pytest.mark.asyncio
    async def test_list_forwards_page_token_and_clamps_page_size(self):
        service = _service({"revisions": [_revision()]})

        await _manage_drive_revisions_impl(
            service, EMAIL, "f1", "list", page_size=5000, page_token="tok"
        )

        kwargs = service.revisions.return_value.list.call_args.kwargs
        assert kwargs["pageSize"] == 1000
        assert kwargs["pageToken"] == "tok"

    @pytest.mark.asyncio
    async def test_list_empty(self):
        result = await _manage_drive_revisions_impl(_service(), EMAIL, "f1", "list")
        assert result.startswith("No revisions found for file f1")


class TestSingleRevision:
    @pytest.mark.asyncio
    async def test_get(self):
        service = _service(get_response=_revision("r9", md5Checksum="abc"))

        result = await _manage_drive_revisions_impl(
            service, EMAIL, "f1", "get", revision_id="r9"
        )

        service.revisions.return_value.get.assert_called_once_with(
            fileId="f1", revisionId="r9", fields=REVISION_FIELDS
        )
        assert "Revision ID: r9" in result
        assert "MD5: abc" in result

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("action", "keep"), [("keep_forever", True), ("unkeep", False)]
    )
    async def test_keep_forever_toggle(self, action, keep):
        service = _service(update_response=_revision("r1", keepForever=keep))

        result = await _manage_drive_revisions_impl(
            service, EMAIL, "f1", action, revision_id="r1"
        )

        service.revisions.return_value.update.assert_called_once_with(
            fileId="f1",
            revisionId="r1",
            body={"keepForever": keep},
            fields=REVISION_FIELDS,
        )
        assert ("kept forever" in result) is keep
        assert f"Keep forever: {'yes' if keep else 'no'}" in result

    @pytest.mark.asyncio
    async def test_delete(self):
        service = _service()

        result = await _manage_drive_revisions_impl(
            service, EMAIL, "f1", "delete", revision_id="r1"
        )

        service.revisions.return_value.delete.assert_called_once_with(
            fileId="f1", revisionId="r1"
        )
        assert "Revision r1 of file f1 permanently deleted" in result

    @pytest.mark.asyncio
    async def test_delete_respects_permission_denial(self):
        service = _service()
        with patch("gdrive.drive_helpers.is_action_denied", return_value=True):
            with pytest.raises(UserInputError, match="not allowed"):
                await _manage_drive_revisions_impl(
                    service, EMAIL, "f1", "delete", revision_id="r1"
                )
        service.revisions.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", [a for a in REVISION_ACTIONS if a != "list"])
    async def test_revision_id_required(self, action):
        with pytest.raises(UserInputError, match="revision_id is required"):
            await _manage_drive_revisions_impl(_service(), EMAIL, "f1", action)

    @pytest.mark.asyncio
    async def test_invalid_action(self):
        with pytest.raises(UserInputError, match="Invalid action 'nope'"):
            await _manage_drive_revisions_impl(_service(), EMAIL, "f1", "nope")

    @pytest.mark.asyncio
    async def test_file_id_required(self):
        with pytest.raises(UserInputError, match="file_id is required"):
            await _manage_drive_revisions_impl(_service(), EMAIL, "", "list")


class TestDownload:
    @pytest.mark.asyncio
    async def test_download_text_revision(self):
        service = _service(
            get_response=_revision("r1", mimeType="text/plain", size="5")
        )
        with patch(
            "gdrive.drive_helpers.download_media_bytes",
            new=AsyncMock(return_value=b"hello"),
        ) as download:
            result = await _manage_drive_revisions_impl(
                service, EMAIL, "f1", "download", revision_id="r1"
            )

        service.revisions.return_value.get_media.assert_called_once_with(
            fileId="f1", revisionId="r1"
        )
        assert download.call_args.kwargs["kind"] == "revision"
        assert download.call_args.kwargs["file_id"] == "f1"
        assert result.endswith("--- CONTENT ---\nhello")
        assert "Size: 5 B" in result

    @pytest.mark.asyncio
    async def test_download_declared_oversized_is_rejected_before_media_call(
        self, monkeypatch
    ):
        monkeypatch.setenv("WORKSPACE_MCP_MAX_FILE_BYTES", "100")
        service = _service(
            get_response=_revision(
                "r1",
                mimeType="application/octet-stream",
                size="500",
                originalFilename="big.bin",
            )
        )

        result = await _manage_drive_revisions_impl(
            service, EMAIL, "f1", "download", revision_id="r1"
        )

        assert result.startswith("Error:")
        assert "big.bin" in result
        assert "WORKSPACE_MCP_MAX_FILE_BYTES" in result
        service.revisions.return_value.get_media.assert_not_called()

    @pytest.mark.asyncio
    async def test_download_streaming_cap_error_is_returned_as_text(self):
        from core.file_limits import FileTooLargeError

        service = _service(get_response=_revision("r1", mimeType="text/plain"))
        with patch(
            "gdrive.drive_helpers.download_media_bytes",
            new=AsyncMock(side_effect=FileTooLargeError("Error: too big")),
        ):
            result = await _manage_drive_revisions_impl(
                service, EMAIL, "f1", "download", revision_id="r1"
            )
        assert result == "Error: too big"

    @pytest.mark.asyncio
    async def test_download_google_native_returns_export_links(self):
        service = _service(
            get_response=_revision(
                "r1",
                mimeType="application/vnd.google-apps.document",
                size=None,
                exportLinks={
                    "application/pdf": "https://docs.google.com/export?format=pdf",
                    "text/plain": "https://docs.google.com/export?format=txt",
                },
            )
        )

        result = await _manage_drive_revisions_impl(
            service, EMAIL, "f1", "download", revision_id="r1"
        )

        service.revisions.return_value.get_media.assert_not_called()
        assert "Google-native revisions have no raw bytes" in result
        assert "- application/pdf: https://docs.google.com/export?format=pdf" in result
        assert "- text/plain: https://docs.google.com/export?format=txt" in result

    @pytest.mark.asyncio
    async def test_download_google_native_without_export_links(self):
        service = _service(
            get_response=_revision(
                "r1", mimeType="application/vnd.google-apps.spreadsheet", size=None
            )
        )
        result = await _manage_drive_revisions_impl(
            service, EMAIL, "f1", "download", revision_id="r1"
        )
        assert "No exportLinks were returned" in result


class TestRenderRevisionContent:
    def test_utf8_text(self):
        assert _render_revision_content(b"caf\xc3\xa9", "text/plain") == "café"

    def test_image_is_base64(self):
        rendered = _render_revision_content(b"\x89PNG", "image/png")
        assert base64.b64encode(b"\x89PNG").decode() in rendered
        assert "image/png" in rendered

    def test_binary_fallback_mentions_size_and_md5(self):
        rendered = _render_revision_content(b"\xff\xfe\x00", "application/octet-stream")
        assert "3 bytes" in rendered
        assert "md5" in rendered

    def test_pdf_uses_extractor(self):
        with patch("gdrive.drive_helpers.extract_pdf_text", return_value="pdf text"):
            assert _render_revision_content(b"%PDF", "application/pdf") == "pdf text"

    def test_pdf_without_text(self):
        with patch("gdrive.drive_helpers.extract_pdf_text", return_value=None):
            rendered = _render_revision_content(b"%PDF", "application/pdf")
        assert "Could not extract text from PDF" in rendered

    def test_office_uses_extractor(self):
        mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        with patch("gdrive.drive_helpers.extract_office_xml_text", return_value="docx"):
            assert _render_revision_content(b"PK", mime) == "docx"


@pytest.mark.asyncio
async def test_tool_delegates_to_helper():
    service = _service({"revisions": [_revision()]})

    result = await _unwrap(manage_drive_revisions)(
        service=service, user_google_email=EMAIL, file_id="f1", action="list"
    )

    assert "Found 1 revisions for file f1" in result
