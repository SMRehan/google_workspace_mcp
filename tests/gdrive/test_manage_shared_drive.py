"""Unit tests for manage_shared_drive."""

import os
import sys
import uuid
from unittest.mock import AsyncMock, Mock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from core.utils import UserInputError
from gdrive.drive_helpers import (
    SHARED_DRIVE_ACTIONS,
    SHARED_DRIVE_FIELDS,
    _manage_shared_drive_impl,
    _validate_shared_drive_restrictions,
)
from gdrive.drive_tools import manage_shared_drive

EMAIL = "user@example.com"


def _unwrap(tool):
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _drive(**extra):
    base = {
        "id": "sd1",
        "name": "Finance",
        "createdTime": "2026-01-01T00:00:00Z",
        "hidden": False,
        "restrictions": {"domainUsersOnly": True, "driveMembersOnly": False},
        "capabilities": {"canEdit": True, "canDeleteDrive": False},
    }
    base.update(extra)
    return base


def _service(payload=None):
    service = Mock()
    drives = service.drives.return_value
    for method in ("get", "create", "update", "hide", "unhide"):
        getattr(drives, method).return_value.execute.return_value = payload or _drive()
    drives.delete.return_value.execute.return_value = ""
    return service


class TestList:
    @pytest.mark.asyncio
    async def test_list_delegates_to_existing_helper(self):
        service = _service()
        with patch(
            "gdrive.drive_helpers._list_shared_drives_impl",
            new=AsyncMock(return_value="listed"),
        ) as listed:
            result = await _manage_shared_drive_impl(
                service,
                EMAIL,
                "list",
                page_size=10,
                page_token="t",
                query="name contains 'F'",
            )
        listed.assert_awaited_once_with(
            service, EMAIL, page_size=10, page_token="t", query="name contains 'F'"
        )
        assert result == "listed"


class TestCreate:
    @pytest.mark.asyncio
    async def test_create_generates_uuid_request_id(self):
        service = _service()

        result = await _manage_shared_drive_impl(
            service, EMAIL, "create", name="  Finance "
        )

        kwargs = service.drives.return_value.create.call_args.kwargs
        uuid.UUID(kwargs["requestId"])  # raises if not a valid UUID
        assert kwargs["body"] == {"name": "Finance"}
        assert kwargs["fields"] == SHARED_DRIVE_FIELDS
        assert f"request ID {kwargs['requestId']}" in result
        assert "Name: Finance" in result
        assert "Restrictions: domainUsersOnly" in result
        assert "Capabilities: canEdit" in result

    @pytest.mark.asyncio
    async def test_create_with_restrictions(self):
        service = _service()

        await _manage_shared_drive_impl(
            service,
            EMAIL,
            "create",
            name="X",
            restrictions={"driveMembersOnly": True},
        )

        body = service.drives.return_value.create.call_args.kwargs["body"]
        assert body == {"name": "X", "restrictions": {"driveMembersOnly": True}}

    @pytest.mark.asyncio
    async def test_create_requires_name(self):
        with pytest.raises(UserInputError, match="name is required"):
            await _manage_shared_drive_impl(_service(), EMAIL, "create", name=" ")


class TestGetUpdate:
    @pytest.mark.asyncio
    async def test_get(self):
        service = _service()

        result = await _manage_shared_drive_impl(service, EMAIL, "get", drive_id="sd1")

        service.drives.return_value.get.assert_called_once_with(
            driveId="sd1", fields=SHARED_DRIVE_FIELDS
        )
        assert "Hidden: no" in result
        assert "Created: 2026-01-01T00:00:00Z" in result

    @pytest.mark.asyncio
    async def test_get_with_domain_admin_access(self):
        service = _service()

        await _manage_shared_drive_impl(
            service, EMAIL, "get", drive_id="sd1", use_domain_admin_access=True
        )

        kwargs = service.drives.return_value.get.call_args.kwargs
        assert kwargs["useDomainAdminAccess"] is True

    @pytest.mark.asyncio
    async def test_update_name_and_restrictions(self):
        service = _service()

        result = await _manage_shared_drive_impl(
            service,
            EMAIL,
            "update",
            drive_id="sd1",
            name="Renamed",
            restrictions={
                "copyRequiresWriterPermission": True,
                "downloadRestriction": {"restrictedForReaders": True},
            },
        )

        kwargs = service.drives.return_value.update.call_args.kwargs
        assert kwargs["driveId"] == "sd1"
        assert kwargs["body"] == {
            "name": "Renamed",
            "restrictions": {
                "copyRequiresWriterPermission": True,
                "downloadRestriction": {"restrictedForReaders": True},
            },
        }
        assert "Shared drive updated." in result

    @pytest.mark.asyncio
    async def test_update_requires_something_to_change(self):
        with pytest.raises(UserInputError, match="name and/or restrictions"):
            await _manage_shared_drive_impl(_service(), EMAIL, "update", drive_id="sd1")

    @pytest.mark.asyncio
    async def test_update_rejects_unknown_restriction(self):
        with pytest.raises(UserInputError, match="Unknown restriction"):
            await _manage_shared_drive_impl(
                _service(), EMAIL, "update", drive_id="sd1", restrictions={"nope": True}
            )


class TestRestrictionValidation:
    def test_rejects_non_dict(self):
        with pytest.raises(UserInputError, match="must be an object"):
            _validate_shared_drive_restrictions(["domainUsersOnly"])

    def test_rejects_non_bool(self):
        with pytest.raises(UserInputError, match="must be true or false"):
            _validate_shared_drive_restrictions({"domainUsersOnly": "yes"})

    def test_download_restriction_must_be_object(self):
        with pytest.raises(
            UserInputError, match="downloadRestriction must be an object"
        ):
            _validate_shared_drive_restrictions({"downloadRestriction": True})


class TestDeleteHide:
    @pytest.mark.asyncio
    async def test_delete_empty_drive(self):
        service = _service()

        result = await _manage_shared_drive_impl(
            service, EMAIL, "delete", drive_id="sd1"
        )

        service.drives.return_value.delete.assert_called_once_with(driveId="sd1")
        assert "permanently deleted" in result
        assert "Only empty shared drives" in result

    @pytest.mark.asyncio
    async def test_delete_with_items_and_admin(self):
        service = _service()

        result = await _manage_shared_drive_impl(
            service,
            EMAIL,
            "delete",
            drive_id="sd1",
            allow_item_deletion=True,
            use_domain_admin_access=True,
        )

        service.drives.return_value.delete.assert_called_once_with(
            driveId="sd1", useDomainAdminAccess=True, allowItemDeletion=True
        )
        assert "contents were deleted too" in result

    @pytest.mark.asyncio
    async def test_delete_respects_permission_denial(self):
        service = _service()
        with patch("gdrive.drive_helpers.is_action_denied", return_value=True):
            with pytest.raises(UserInputError, match="not allowed"):
                await _manage_shared_drive_impl(
                    service, EMAIL, "delete", drive_id="sd1"
                )
        service.drives.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("action", "phrase"),
        [
            ("hide", "hidden from the default view"),
            ("unhide", "restored to the default view"),
        ],
    )
    async def test_hide_unhide(self, action, phrase):
        service = _service(_drive(hidden=(action == "hide")))

        result = await _manage_shared_drive_impl(service, EMAIL, action, drive_id="sd1")

        getattr(service.drives.return_value, action).assert_called_once_with(
            driveId="sd1", fields=SHARED_DRIVE_FIELDS
        )
        assert phrase in result
        assert f"Hidden: {'yes' if action == 'hide' else 'no'}" in result

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "action", [a for a in SHARED_DRIVE_ACTIONS if a not in ("list", "create")]
    )
    async def test_drive_id_required(self, action):
        with pytest.raises(UserInputError, match="drive_id is required"):
            await _manage_shared_drive_impl(_service(), EMAIL, action)

    @pytest.mark.asyncio
    async def test_invalid_action(self):
        with pytest.raises(UserInputError, match="Invalid action 'rename'"):
            await _manage_shared_drive_impl(_service(), EMAIL, "rename", drive_id="sd1")


@pytest.mark.asyncio
async def test_tool_delegates_to_helper():
    service = _service()

    result = await _unwrap(manage_shared_drive)(
        service=service, user_google_email=EMAIL, action="get", drive_id="sd1"
    )

    assert "Name: Finance" in result
