"""Tests for manage_sheet_protection."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from core.utils import UserInputError
from gsheets import sheets_tools
from tests.gsheets._batch_helpers import (
    EMAIL,
    SPREADSHEET,
    TWO_SHEETS,
    get_fields,
    sent_request,
    service_with,
    unwrap,
)

GRID = {
    "sheetId": 0,
    "startRowIndex": 0,
    "endRowIndex": 20,
    "startColumnIndex": 0,
    "endColumnIndex": 4,
}


async def _call(service, **kwargs):
    return await unwrap(sheets_tools.manage_sheet_protection)(
        service=service,
        user_google_email=EMAIL,
        spreadsheet_id=SPREADSHEET,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_add_range_with_editors_defaults_to_restricted():
    service = service_with(
        TWO_SHEETS,
        replies=[{"addProtectedRange": {"protectedRange": {"protectedRangeId": 88}}}],
    )
    result = await _call(
        service,
        action="add",
        range_name="Data!A1:D20",
        description="Totals",
        editors=["a@example.com", " b@example.com "],
        editor_groups=["finance@example.com"],
        domain_users_can_edit=False,
    )
    sent = sent_request(service)["addProtectedRange"]["protectedRange"]
    assert sent == {
        "range": GRID,
        "description": "Totals",
        "warningOnly": False,
        "editors": {
            "users": ["a@example.com", "b@example.com"],
            "groups": ["finance@example.com"],
            "domainUsersCanEdit": False,
        },
    }
    assert "protected range 'Data!A1:D20' [restricted] (ID: 88)" in result


@pytest.mark.asyncio
async def test_add_whole_sheet_warning_only_with_unprotected_ranges():
    service = service_with(TWO_SHEETS)
    result = await _call(
        service,
        action="add",
        sheet_name="Summary",
        warning_only=True,
        unprotected_ranges=["A1:A5"],
    )
    sent = sent_request(service)["addProtectedRange"]["protectedRange"]
    assert sent["range"] == {"sheetId": 7}
    assert sent["warningOnly"] is True
    assert sent["unprotectedRanges"] == [
        {
            "sheetId": 7,
            "startRowIndex": 0,
            "endRowIndex": 5,
            "startColumnIndex": 0,
            "endColumnIndex": 1,
        }
    ]
    assert "editors" not in sent
    assert "protected sheet 'Summary' [warning only]" in result


@pytest.mark.asyncio
async def test_warning_only_with_editors_is_refused():
    service = service_with(TWO_SHEETS)
    with pytest.raises(
        UserInputError, match="warning-only protection cannot have editors"
    ):
        await _call(
            service,
            action="add",
            range_name="A1:B2",
            warning_only=True,
            editors=["a@example.com"],
        )
    service.spreadsheets.return_value.batchUpdate.assert_not_called()


@pytest.mark.asyncio
async def test_update_sends_only_supplied_fields():
    service = service_with(TWO_SHEETS)
    result = await _call(
        service,
        action="update",
        protected_range_id=88,
        description="Locked",
        editors=["c@example.com"],
    )
    sent = sent_request(service)["updateProtectedRange"]
    assert sent["fields"] == "description,editors"
    assert sent["protectedRange"] == {
        "protectedRangeId": 88,
        "description": "Locked",
        "editors": {"users": ["c@example.com"]},
    }
    assert "updated protected range 88 (description, editors)" in result


@pytest.mark.asyncio
async def test_update_can_move_the_range():
    service = service_with(TWO_SHEETS)
    await _call(
        service, action="update", protected_range_id=88, range_name="Data!A1:D20"
    )
    sent = sent_request(service)["updateProtectedRange"]
    assert sent["fields"] == "range"
    assert sent["protectedRange"]["range"] == GRID


@pytest.mark.asyncio
async def test_update_with_nothing_to_change_is_refused():
    service = service_with(TWO_SHEETS)
    with pytest.raises(UserInputError, match="at least one of range_name"):
        await _call(service, action="update", protected_range_id=88)


@pytest.mark.asyncio
async def test_delete_by_id():
    service = service_with(TWO_SHEETS)
    result = await _call(service, action="delete", protected_range_id=88)
    assert sent_request(service) == {"deleteProtectedRange": {"protectedRangeId": 88}}
    service.spreadsheets.return_value.get.assert_not_called()
    assert "deleted protected range 88" in result


@pytest.mark.asyncio
async def test_list_formats_scope_mode_editors_and_exceptions():
    service = service_with(
        [
            {
                "properties": {"sheetId": 0, "title": "Data"},
                "protectedRanges": [
                    {
                        "protectedRangeId": 1,
                        "range": GRID,
                        "description": "Totals",
                        "editors": {
                            "users": ["a@example.com"],
                            "domainUsersCanEdit": True,
                        },
                    },
                    {
                        "protectedRangeId": 2,
                        "range": {"sheetId": 0},
                        "warningOnly": True,
                        "unprotectedRanges": [
                            {
                                "sheetId": 0,
                                "startRowIndex": 0,
                                "endRowIndex": 1,
                                "startColumnIndex": 0,
                                "endColumnIndex": 1,
                            }
                        ],
                    },
                    {"protectedRangeId": 3, "namedRangeId": "nr9"},
                ],
            },
            {"properties": {"sheetId": 7, "title": "Summary"}},
        ]
    )
    result = await _call(service, action="list")
    assert get_fields(service) == "sheets(properties(sheetId,title),protectedRanges)"
    assert (
        "ID 1: Data!A1:D20 [restricted] - Totals (users: a@example.com; domain users can edit)"
        in result
    )
    assert "ID 2: Data [warning only] except Data!A1" in result
    assert "ID 3: named range nr9 [restricted]" in result
    service.spreadsheets.return_value.batchUpdate.assert_not_called()


@pytest.mark.asyncio
async def test_list_with_nothing_protected():
    service = service_with(TWO_SHEETS)
    assert "No protected ranges found." in await _call(service, action="list")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"action": "lock"}, "Invalid action"),
        ({"action": "update"}, "protected_range_id is required"),
        ({"action": "delete"}, "protected_range_id is required"),
        ({"action": "add", "sheet_name": "Nope"}, "Sheet 'Nope' not found"),
    ],
)
async def test_invalid_input_is_refused(kwargs, message):
    service = service_with(TWO_SHEETS)
    with pytest.raises(UserInputError, match=message):
        await _call(service, **kwargs)
    service.spreadsheets.return_value.batchUpdate.assert_not_called()
