"""Tests for find_replace_sheet."""

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
    sent_request,
    service_with,
    unwrap,
)

REPLY = {
    "findReplace": {
        "valuesChanged": 4,
        "formulasChanged": 1,
        "rowsChanged": 3,
        "sheetsChanged": 1,
        "occurrencesChanged": 5,
    }
}


async def _call(service, **kwargs):
    return await unwrap(sheets_tools.find_replace_sheet)(
        service=service,
        user_google_email=EMAIL,
        spreadsheet_id=SPREADSHEET,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_defaults_target_first_sheet_with_plain_match():
    service = service_with(TWO_SHEETS, replies=[REPLY])
    result = await _call(service, find="foo", replacement="bar")
    assert sent_request(service) == {
        "findReplace": {
            "find": "foo",
            "replacement": "bar",
            "matchCase": False,
            "matchEntireCell": False,
            "searchByRegex": False,
            "includeFormulas": False,
            "sheetId": 0,
        }
    }
    assert "in sheet 'Data'" in result
    assert (
        "5 occurrence(s) changed across 4 value cell(s) and 1 formula cell(s) in 3 row(s) on 1 sheet(s)"
        in result
    )


@pytest.mark.asyncio
async def test_sheet_name_selects_sheet_id():
    service = service_with(TWO_SHEETS, replies=[REPLY])
    await _call(
        service,
        find="foo",
        sheet_name="Summary",
        match_case=True,
        include_formulas=True,
    )
    sent = sent_request(service)["findReplace"]
    assert sent["sheetId"] == 7
    assert sent["matchCase"] is True
    assert sent["includeFormulas"] is True
    assert sent["replacement"] == ""


@pytest.mark.asyncio
async def test_range_name_wins_over_all_sheets():
    service = service_with(TWO_SHEETS, replies=[REPLY])
    result = await _call(
        service,
        find="foo",
        range_name="B2:B500",
        sheet_name="Summary",
        all_sheets=True,
        match_entire_cell=True,
    )
    sent = sent_request(service)["findReplace"]
    assert sent["range"] == {
        "sheetId": 7,
        "startRowIndex": 1,
        "endRowIndex": 500,
        "startColumnIndex": 1,
        "endColumnIndex": 2,
    }
    assert "allSheets" not in sent and "sheetId" not in sent
    assert sent["matchEntireCell"] is True
    assert "in range 'B2:B500'" in result


@pytest.mark.asyncio
async def test_all_sheets_skips_the_metadata_fetch():
    service = service_with(TWO_SHEETS, replies=[REPLY])
    result = await _call(
        service,
        find="(\\d+)",
        replacement="$1 units",
        all_sheets=True,
        search_by_regex=True,
    )
    sent = sent_request(service)["findReplace"]
    assert sent["allSheets"] is True
    assert sent["searchByRegex"] is True
    service.spreadsheets.return_value.get.assert_not_called()
    assert "in all sheets" in result


@pytest.mark.asyncio
async def test_empty_reply_reports_zero_changes():
    service = service_with(TWO_SHEETS, replies=[{}])
    result = await _call(service, find="foo")
    assert "0 occurrence(s) changed" in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"find": ""}, "find must be a non-empty string"),
        (
            {"find": "(unclosed", "search_by_regex": True},
            "not a valid regular expression",
        ),
        ({"find": "x", "sheet_name": "Nope"}, "Sheet 'Nope' not found"),
    ],
)
async def test_invalid_input_is_refused(kwargs, message):
    service = service_with(TWO_SHEETS)
    with pytest.raises(UserInputError, match=message):
        await _call(service, **kwargs)
    service.spreadsheets.return_value.batchUpdate.assert_not_called()
