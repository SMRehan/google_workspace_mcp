"""Unit tests for the manage_drive_file tool and its helper."""

import os
import sys
from unittest.mock import AsyncMock, Mock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from core.utils import UserInputError
from gdrive.drive_helpers import FILE_MANAGEMENT_ACTIONS, _manage_drive_file_impl
from gdrive.drive_tools import manage_drive_file

EMAIL = "user@example.com"


def _unwrap(tool):
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _service(update_response=None):
    service = Mock()
    service.files().update().execute.return_value = update_response or {
        "id": "f1",
        "name": "Report.pdf",
        "mimeType": "application/pdf",
        "parents": ["p1"],
        "webViewLink": "https://drive.google.com/file/d/f1",
    }
    service.files().delete().execute.return_value = ""
    service.files().emptyTrash().execute.return_value = ""
    service.files.reset_mock()
    return service


def _update_kwargs(service):
    return service.files.return_value.update.call_args.kwargs


class TestFieldToggles:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("action", "field", "value"),
        [
            ("trash", "trashed", True),
            ("untrash", "trashed", False),
            ("star", "starred", True),
            ("unstar", "starred", False),
            ("restrict_download", "copyRequiresWriterPermission", True),
            ("allow_download", "copyRequiresWriterPermission", False),
        ],
    )
    async def test_toggle_sends_single_field_patch(self, action, field, value):
        service = _service()

        result = await _manage_drive_file_impl(service, EMAIL, action, file_id="f1")

        kwargs = _update_kwargs(service)
        assert kwargs["fileId"] == "f1"
        assert kwargs["body"] == {field: value}
        assert kwargs["supportsAllDrives"] is True
        assert "Name: Report.pdf" in result
        assert "ID: f1" in result

    @pytest.mark.asyncio
    async def test_trash_reports_flag_from_response(self):
        service = _service({"id": "f1", "name": "x", "trashed": True})

        result = await _manage_drive_file_impl(service, EMAIL, "trash", file_id="f1")

        assert "moved to trash" in result
        assert "Flags: trashed" in result

    @pytest.mark.asyncio
    async def test_action_is_case_insensitive(self):
        service = _service()

        await _manage_drive_file_impl(service, EMAIL, " Star ", file_id="f1")

        assert _update_kwargs(service)["body"] == {"starred": True}


class TestRenameAndDescription:
    @pytest.mark.asyncio
    async def test_rename(self):
        service = _service({"id": "f1", "name": "New name"})

        result = await _manage_drive_file_impl(
            service, EMAIL, "rename", file_id="f1", new_name="  New name "
        )

        assert _update_kwargs(service)["body"] == {"name": "New name"}
        assert "File renamed." in result
        assert "Name: New name" in result

    @pytest.mark.asyncio
    async def test_rename_requires_new_name(self):
        with pytest.raises(UserInputError, match="new_name is required"):
            await _manage_drive_file_impl(_service(), EMAIL, "rename", file_id="f1")

    @pytest.mark.asyncio
    async def test_set_description_allows_empty_string_to_clear(self):
        service = _service()

        await _manage_drive_file_impl(
            service, EMAIL, "set_description", file_id="f1", description=""
        )

        assert _update_kwargs(service)["body"] == {"description": ""}

    @pytest.mark.asyncio
    async def test_set_description_requires_description(self):
        with pytest.raises(UserInputError, match="description is required"):
            await _manage_drive_file_impl(
                _service(), EMAIL, "set_description", file_id="f1"
            )


class TestMove:
    @pytest.mark.asyncio
    async def test_move_uses_add_and_remove_parents(self):
        service = _service({"id": "f1", "name": "x", "parents": ["dest"]})

        with (
            patch(
                "gdrive.drive_helpers.resolve_drive_item",
                new=AsyncMock(return_value=("f1", {"name": "x", "parents": ["old"]})),
            ) as resolve_item,
            patch(
                "gdrive.drive_helpers.resolve_folder_id",
                new=AsyncMock(return_value="dest"),
            ) as resolve_folder,
        ):
            result = await _manage_drive_file_impl(
                service,
                EMAIL,
                "move",
                file_id="f1",
                new_parent_folder_id="dest-or-shortcut",
            )

        # Shortcuts are acted on directly, never followed.
        assert resolve_item.call_args.kwargs["follow_shortcuts"] is False
        resolve_folder.assert_awaited_once_with(service, "dest-or-shortcut")
        kwargs = _update_kwargs(service)
        assert kwargs["addParents"] == "dest"
        assert kwargs["removeParents"] == "old"
        assert kwargs["body"] == {}
        assert kwargs["supportsAllDrives"] is True
        assert "moved from old to dest" in result

    @pytest.mark.asyncio
    async def test_move_with_no_current_parents_omits_remove_parents(self):
        service = _service({"id": "f1", "name": "x", "parents": ["dest"]})

        with (
            patch(
                "gdrive.drive_helpers.resolve_drive_item",
                new=AsyncMock(return_value=("f1", {"name": "x"})),
            ),
            patch(
                "gdrive.drive_helpers.resolve_folder_id",
                new=AsyncMock(return_value="dest"),
            ),
        ):
            await _manage_drive_file_impl(
                service, EMAIL, "move", file_id="f1", new_parent_folder_id="dest"
            )

        assert "removeParents" not in _update_kwargs(service)

    @pytest.mark.asyncio
    async def test_move_to_current_parent_is_a_noop(self):
        service = _service()

        with (
            patch(
                "gdrive.drive_helpers.resolve_drive_item",
                new=AsyncMock(return_value=("f1", {"name": "x", "parents": ["dest"]})),
            ),
            patch(
                "gdrive.drive_helpers.resolve_folder_id",
                new=AsyncMock(return_value="dest"),
            ),
        ):
            result = await _manage_drive_file_impl(
                service, EMAIL, "move", file_id="f1", new_parent_folder_id="dest"
            )

        assert "already in folder dest" in result
        service.files.return_value.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_move_requires_destination(self):
        with pytest.raises(UserInputError, match="new_parent_folder_id is required"):
            await _manage_drive_file_impl(_service(), EMAIL, "move", file_id="f1")


class TestDestructive:
    @pytest.mark.asyncio
    async def test_delete_bypasses_trash_with_shared_drive_support(self):
        service = _service()

        result = await _manage_drive_file_impl(service, EMAIL, "delete", file_id="f1")

        service.files.return_value.delete.assert_called_once_with(
            fileId="f1", supportsAllDrives=True
        )
        assert "permanently deleted" in result
        service.files.return_value.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_trash_my_drive(self):
        service = _service()

        result = await _manage_drive_file_impl(service, EMAIL, "empty_trash")

        service.files.return_value.emptyTrash.assert_called_once_with()
        assert "My Drive" in result

    @pytest.mark.asyncio
    async def test_empty_trash_shared_drive(self):
        service = _service()

        result = await _manage_drive_file_impl(
            service, EMAIL, "empty_trash", drive_id="sd1"
        )

        service.files.return_value.emptyTrash.assert_called_once_with(driveId="sd1")
        assert "shared drive sd1" in result

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ["delete", "empty_trash", "trash"])
    async def test_denied_actions_are_refused_before_any_api_call(self, action):
        service = _service()

        with patch(
            "gdrive.drive_helpers.is_action_denied", return_value=True
        ) as denied:
            with pytest.raises(UserInputError, match="not allowed"):
                await _manage_drive_file_impl(service, EMAIL, action, file_id="f1")

        denied.assert_called_once_with("drive", action)
        service.files.assert_not_called()


class TestValidation:
    @pytest.mark.asyncio
    async def test_invalid_action_lists_valid_ones(self):
        with pytest.raises(UserInputError, match="Invalid action 'bogus'") as exc:
            await _manage_drive_file_impl(_service(), EMAIL, "bogus", file_id="f1")
        for action in FILE_MANAGEMENT_ACTIONS:
            assert action in str(exc.value)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "action", [a for a in FILE_MANAGEMENT_ACTIONS if a != "empty_trash"]
    )
    async def test_file_id_required_for_all_but_empty_trash(self, action):
        with pytest.raises(UserInputError, match="file_id is required"):
            await _manage_drive_file_impl(_service(), EMAIL, action)


class TestToolWiring:
    @pytest.mark.asyncio
    async def test_tool_delegates_to_helper(self):
        service = _service()

        result = await _unwrap(manage_drive_file)(
            service=service,
            user_google_email=EMAIL,
            action="star",
            file_id="f1",
        )

        assert _update_kwargs(service)["body"] == {"starred": True}
        assert "starred" in result
