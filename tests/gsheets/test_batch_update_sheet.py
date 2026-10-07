"""Tests for the raw batchUpdate passthrough tool."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from core.utils import UserInputError
from gsheets import sheets_tools
from gsheets.sheets_helpers import (
    BATCH_UPDATE_REQUEST_TYPES,
    _summarize_batch_update_reply,
    _validate_batch_update_requests,
)
from tests.gsheets._batch_helpers import (
    EMAIL,
    SPREADSHEET,
    sent_requests,
    service_with,
    unwrap,
)


async def _call(service, requests):
    return await unwrap(sheets_tools.batch_update_sheet)(
        service=service,
        user_google_email=EMAIL,
        spreadsheet_id=SPREADSHEET,
        requests=requests,
    )


def test_request_type_list_matches_discovery_document():
    # 74 request types in the Sheets v4 `Request` schema.
    assert len(BATCH_UPDATE_REQUEST_TYPES) == 74
    assert {"addSheet", "updateCells", "updateTable", "addBanding"} <= (
        BATCH_UPDATE_REQUEST_TYPES
    )


@pytest.mark.asyncio
async def test_requests_pass_through_unchanged_and_replies_are_summarized():
    requests = [
        {"addSheet": {"properties": {"title": "Q3"}}},
        {"deleteDimension": {"range": {"sheetId": 0, "dimension": "ROWS"}}},
        {"addNamedRange": {"namedRange": {"name": "totals"}}},
        {"findReplace": {"find": "a", "replacement": "b", "allSheets": True}},
    ]
    service = service_with(
        replies=[
            {"addSheet": {"properties": {"sheetId": 55, "title": "Q3"}}},
            {},
            {
                "addNamedRange": {
                    "namedRange": {"namedRangeId": "nr1", "name": "totals"}
                }
            },
            {
                "findReplace": {
                    "occurrencesChanged": 3,
                    "valuesChanged": 2,
                    "rowsChanged": 2,
                    "sheetsChanged": 1,
                }
            },
        ]
    )

    result = await _call(service, requests)

    assert sent_requests(service) == requests
    assert "Requests Applied: 4" in result
    assert "Request 1 (addSheet): sheet 'Q3' (sheetId 55)" in result
    assert "Request 2 (deleteDimension): completed" in result
    assert (
        "Request 3 (addNamedRange): named range 'totals' (namedRangeId nr1)" in result
    )
    assert "Request 4 (findReplace): 3 occurrence(s) changed" in result
    assert f"https://docs.google.com/spreadsheets/d/{SPREADSHEET}/edit" in result


@pytest.mark.asyncio
async def test_json_string_requests_are_decoded():
    service = service_with(replies=[{}])
    await _call(service, '[{"repeatCell": {"range": {"sheetId": 0}, "fields": "*"}}]')
    assert sent_requests(service) == [
        {"repeatCell": {"range": {"sheetId": 0}, "fields": "*"}}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "requests, message",
    [
        ([], "non-empty list"),
        ([{"addSheet": {}, "deleteSheet": {}}], "exactly one key"),
        (["addSheet"], "exactly one key"),
        ([{"addSheets": {}}], "unknown batchUpdate request type 'addSheets'"),
        ([{"addSheet": "oops"}], "must be an object"),
        ("not json", "JSON-encoded"),
    ],
)
async def test_invalid_requests_are_refused_before_the_api_call(requests, message):
    service = service_with()
    with pytest.raises(UserInputError, match=message):
        await _call(service, requests)
    service.spreadsheets.return_value.batchUpdate.assert_not_called()


def test_unknown_type_suggests_close_matches():
    with pytest.raises(UserInputError, match="Did you mean: addSheet"):
        _validate_batch_update_requests([{"addSheets": {}}])


@pytest.mark.parametrize(
    "request_type, reply, expected",
    [
        ("addChart", {"addChart": {"chart": {"chartId": 9}}}, "chartId 9"),
        (
            "addFilterView",
            {"addFilterView": {"filter": {"filterViewId": 4, "title": "Open"}}},
            "'Open' (filterViewId 4)",
        ),
        (
            "addProtectedRange",
            {"addProtectedRange": {"protectedRange": {"protectedRangeId": 2}}},
            "protectedRangeId 2",
        ),
        (
            "deleteDuplicates",
            {"deleteDuplicates": {"duplicatesRemovedCount": 5}},
            "5 duplicate row(s)",
        ),
        (
            "trimWhitespace",
            {"trimWhitespace": {"cellsChangedCount": 2}},
            "2 cell(s) changed",
        ),
        (
            "duplicateSheet",
            {"duplicateSheet": {"properties": {"sheetId": 3, "title": "Copy"}}},
            "'Copy' (sheetId 3)",
        ),
        ("updateCells", {}, "completed"),
        ("updateCells", None, "completed"),
    ],
)
def test_reply_summaries_surface_created_ids(request_type, reply, expected):
    assert expected in _summarize_batch_update_reply(request_type, reply)
