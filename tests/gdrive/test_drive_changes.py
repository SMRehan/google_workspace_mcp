"""Unit tests for list_drive_changes."""

import os
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from core.utils import UserInputError
from gdrive.drive_helpers import CHANGES_FIELDS, _list_drive_changes_impl
from gdrive.drive_tools import list_drive_changes

EMAIL = "user@example.com"


def _unwrap(tool):
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _service(start_token="100", list_response=None):
    service = Mock()
    changes = service.changes.return_value
    changes.getStartPageToken.return_value.execute.return_value = {
        "startPageToken": start_token
    }
    changes.list.return_value.execute.return_value = list_response or {}
    return service


class TestGetStartToken:
    @pytest.mark.asyncio
    async def test_all_drives(self):
        service = _service("4242")

        result = await _list_drive_changes_impl(service, EMAIL, "get_start_token")

        service.changes.return_value.getStartPageToken.assert_called_once_with(
            supportsAllDrives=True
        )
        assert "Start page token for all drives: 4242" in result

    @pytest.mark.asyncio
    async def test_single_shared_drive(self):
        service = _service("7")

        result = await _list_drive_changes_impl(
            service, EMAIL, "get_start_token", drive_id="sd1"
        )

        service.changes.return_value.getStartPageToken.assert_called_once_with(
            supportsAllDrives=True, driveId="sd1"
        )
        assert "shared drive sd1: 7" in result


class TestList:
    @pytest.mark.asyncio
    async def test_list_requires_page_token(self):
        with pytest.raises(UserInputError, match="page_token is required"):
            await _list_drive_changes_impl(_service(), EMAIL, "list")

    @pytest.mark.asyncio
    async def test_list_request_parameters(self):
        service = _service(list_response={"changes": [], "newStartPageToken": "200"})

        await _list_drive_changes_impl(
            service, EMAIL, "list", page_token="100", drive_id="sd1", page_size=25
        )

        kwargs = service.changes.return_value.list.call_args.kwargs
        assert kwargs == {
            "pageToken": "100",
            "pageSize": 25,
            "includeRemoved": True,
            "supportsAllDrives": True,
            "includeItemsFromAllDrives": True,
            "fields": CHANGES_FIELDS,
            "driveId": "sd1",
        }

    @pytest.mark.asyncio
    async def test_list_optional_flags(self):
        service = _service(list_response={"changes": [], "newStartPageToken": "200"})

        await _list_drive_changes_impl(
            service,
            EMAIL,
            "list",
            page_token="100",
            include_removed=False,
            restrict_to_my_drive=True,
            page_size=-5,
        )

        kwargs = service.changes.return_value.list.call_args.kwargs
        assert kwargs["includeRemoved"] is False
        assert kwargs["restrictToMyDrive"] is True
        assert kwargs["pageSize"] == 1
        assert "driveId" not in kwargs

    @pytest.mark.asyncio
    async def test_list_formats_file_drive_and_removed_changes(self):
        service = _service(
            list_response={
                "changes": [
                    {
                        "changeType": "file",
                        "fileId": "f1",
                        "time": "2026-01-01T00:00:00Z",
                        "removed": False,
                        "driveId": "sd1",
                        "file": {
                            "id": "f1",
                            "name": "Budget",
                            "mimeType": "application/vnd.google-apps.spreadsheet",
                            "trashed": True,
                        },
                    },
                    {
                        "changeType": "file",
                        "fileId": "f2",
                        "time": "2026-01-01T00:01:00Z",
                        "removed": True,
                    },
                    {
                        "changeType": "drive",
                        "driveId": "sd2",
                        "time": "2026-01-01T00:02:00Z",
                        "removed": False,
                        "drive": {"id": "sd2", "name": "Finance"},
                    },
                    {
                        "changeType": "drive",
                        "driveId": "sd3",
                        "time": "2026-01-01T00:03:00Z",
                        "removed": True,
                    },
                ],
                "newStartPageToken": "300",
            }
        )

        result = await _list_drive_changes_impl(
            service, EMAIL, "list", page_token="100"
        )

        lines = result.splitlines()
        assert lines[0] == "4 change(s) since token 100:"
        assert lines[1] == (
            '- [2026-01-01T00:00:00Z] "Budget" (ID: f1, '
            "Type: application/vnd.google-apps.spreadsheet, shared drive sd1) [trashed]"
        )
        assert lines[2] == (
            "- [2026-01-01T00:01:00Z] file f2 removed (deleted permanently or access lost)"
        )
        assert lines[3] == (
            '- [2026-01-01T00:02:00Z] shared drive "Finance" (ID: sd2) changed'
        )
        assert lines[4] == (
            "- [2026-01-01T00:03:00Z] shared drive sd3 removed (deleted or access lost)"
        )
        assert "newStartPageToken: 300" in result
        assert "nextPageToken" not in result

    @pytest.mark.asyncio
    async def test_list_surfaces_next_page_token_when_more_remain(self):
        service = _service(list_response={"changes": [], "nextPageToken": "150"})

        result = await _list_drive_changes_impl(
            service, EMAIL, "list", page_token="100"
        )

        assert "0 change(s) since token 100:" in result
        assert "nextPageToken: 150" in result
        assert "newStartPageToken" not in result

    @pytest.mark.asyncio
    async def test_invalid_action(self):
        with pytest.raises(UserInputError, match="Invalid action 'watch'"):
            await _list_drive_changes_impl(_service(), EMAIL, "watch")


@pytest.mark.asyncio
async def test_tool_defaults_to_list_and_delegates():
    service = _service(list_response={"changes": [], "newStartPageToken": "9"})

    result = await _unwrap(list_drive_changes)(
        service=service, user_google_email=EMAIL, page_token="8"
    )

    assert "newStartPageToken: 9" in result
