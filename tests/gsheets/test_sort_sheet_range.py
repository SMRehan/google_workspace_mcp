"""Tests for sort_sheet_range."""

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


async def _call(service, **kwargs):
    return await unwrap(sheets_tools.sort_sheet_range)(
        service=service,
        user_google_email=EMAIL,
        spreadsheet_id=SPREADSHEET,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_multi_key_sort_builds_sort_range_request():
    service = service_with(TWO_SHEETS)
    result = await _call(
        service,
        range_name="Data!B2:F200",
        sort_specs=[{"column": "D", "order": "DESCENDING"}, {"column": "B"}, "F"],
    )
    assert sent_request(service) == {
        "sortRange": {
            "range": {
                "sheetId": 0,
                "startRowIndex": 1,
                "endRowIndex": 200,
                "startColumnIndex": 1,
                "endColumnIndex": 6,
            },
            "sortSpecs": [
                {"dimensionIndex": 3, "sortOrder": "DESCENDING"},
                {"dimensionIndex": 1, "sortOrder": "ASCENDING"},
                {"dimensionIndex": 5, "sortOrder": "ASCENDING"},
            ],
        }
    }
    assert (
        "sorted range 'Data!B2:F200' by D descending, B ascending, F ascending"
        in result
    )


@pytest.mark.asyncio
async def test_has_header_skips_the_first_row():
    service = service_with(TWO_SHEETS)
    result = await _call(
        service,
        range_name="A1:C10",
        sort_specs='[{"column": 2, "order": "asc"}]',
        has_header=True,
    )
    sent = sent_request(service)["sortRange"]
    assert sent["range"]["startRowIndex"] == 1
    assert sent["range"]["endRowIndex"] == 10
    assert sent["sortSpecs"] == [{"dimensionIndex": 2, "sortOrder": "ASCENDING"}]
    assert "(header row kept)" in result


@pytest.mark.asyncio
async def test_has_header_on_a_single_row_range_is_refused():
    service = service_with(TWO_SHEETS)
    with pytest.raises(UserInputError, match="at least two rows"):
        await _call(
            service, range_name="A1:C1", sort_specs=[{"column": "A"}], has_header=True
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"range_name": "", "sort_specs": [{"column": "A"}]}, "range_name is required"),
        ({"range_name": "A1:C10", "sort_specs": []}, "at least one sort spec"),
        (
            {"range_name": "A1:C10", "sort_specs": [{"column": "Z"}]},
            "outside the range's columns",
        ),
        (
            {"range_name": "A1:C10", "sort_specs": [{"order": "ASC"}]},
            "'column' is required",
        ),
        (
            {"range_name": "A1:C10", "sort_specs": [{"column": "1B"}]},
            "not valid letters or an index",
        ),
        ({"range_name": "A1:C10", "sort_specs": [42]}, "must be an object"),
        ({"range_name": "A1:C10", "sort_specs": "nope"}, "JSON-encoded list"),
    ],
)
async def test_invalid_input_is_refused(kwargs, message):
    service = service_with(TWO_SHEETS)
    with pytest.raises(UserInputError, match=message):
        await _call(service, **kwargs)
    service.spreadsheets.return_value.batchUpdate.assert_not_called()
