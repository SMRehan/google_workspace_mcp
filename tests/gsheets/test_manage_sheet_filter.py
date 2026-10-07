"""Tests for manage_sheet_filter: basic filter and filter views."""

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
    "endRowIndex": 200,
    "startColumnIndex": 0,
    "endColumnIndex": 6,
}


async def _call(service, **kwargs):
    return await unwrap(sheets_tools.manage_sheet_filter)(
        service=service,
        user_google_email=EMAIL,
        spreadsheet_id=SPREADSHEET,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_set_basic_builds_range_sort_and_filter_specs():
    service = service_with(TWO_SHEETS)

    result = await _call(
        service,
        action="set_basic",
        range_name="A1:F200",
        sort_specs=[{"column": "C", "order": "desc"}, {"column": 0}],
        filter_specs=[
            {"column": "B", "hidden_values": ["Closed", "Lost"]},
            {
                "column": "D",
                "condition_type": "NUMBER_GREATER",
                "condition_values": [1000],
            },
        ],
    )

    sent = sent_request(service)["setBasicFilter"]["filter"]
    assert sent["range"] == GRID
    assert sent["sortSpecs"] == [
        {"dimensionIndex": 2, "sortOrder": "DESCENDING"},
        {"dimensionIndex": 0, "sortOrder": "ASCENDING"},
    ]
    assert sent["filterSpecs"] == [
        {"columnIndex": 1, "filterCriteria": {"hiddenValues": ["Closed", "Lost"]}},
        {
            "columnIndex": 3,
            "filterCriteria": {
                "condition": {
                    "type": "NUMBER_GREATER",
                    "values": [{"userEnteredValue": "1000"}],
                }
            },
        },
    ]
    assert "title" not in sent
    assert "set the basic filter" in result


@pytest.mark.asyncio
async def test_set_basic_scopes_unprefixed_range_to_sheet_name():
    service = service_with(TWO_SHEETS)
    await _call(service, action="set_basic", sheet_name="Summary", range_name="A1:B2")
    assert sent_request(service)["setBasicFilter"]["filter"]["range"]["sheetId"] == 7


@pytest.mark.asyncio
async def test_clear_basic_targets_the_named_sheet():
    service = service_with(TWO_SHEETS)
    result = await _call(service, action="clear_basic", sheet_name="Summary")
    assert sent_request(service) == {"clearBasicFilter": {"sheetId": 7}}
    assert "cleared the basic filter on sheet 'Summary'" in result


@pytest.mark.asyncio
async def test_create_view_returns_new_id():
    service = service_with(
        TWO_SHEETS,
        replies=[{"addFilterView": {"filter": {"filterViewId": 31, "title": "Open"}}}],
    )
    result = await _call(
        service, action="create_view", title="Open", range_name="Data!A1:F200"
    )
    sent = sent_request(service)["addFilterView"]["filter"]
    assert sent == {"range": GRID, "title": "Open"}
    assert "created filter view 'Open' (ID: 31)" in result


@pytest.mark.asyncio
async def test_update_view_sends_only_supplied_fields():
    service = service_with(TWO_SHEETS)
    await _call(
        service,
        action="update_view",
        filter_view_id=31,
        range_name="Data!A1:F200",
        title="Open deals",
    )
    sent = sent_request(service)["updateFilterView"]
    assert sent["fields"] == "range,title"
    assert sent["filter"] == {"filterViewId": 31, "range": GRID, "title": "Open deals"}


@pytest.mark.asyncio
async def test_update_view_without_range_uses_the_existing_view_range_for_validation():
    sheets_with_views = [
        {
            "properties": {"sheetId": 0, "title": "Data"},
            "filterViews": [{"filterViewId": 31, "range": GRID}],
        }
    ]
    service = service_with(sheets_with_views)
    await _call(
        service,
        action="update_view",
        filter_view_id=31,
        sort_specs=[{"column": "F"}],
    )
    sent = sent_request(service)["updateFilterView"]
    assert sent["fields"] == "sortSpecs"
    assert sent["filter"] == {
        "filterViewId": 31,
        "sortSpecs": [{"dimensionIndex": 5, "sortOrder": "ASCENDING"}],
    }

    service = service_with(sheets_with_views)
    with pytest.raises(UserInputError, match="outside the range's columns"):
        await _call(
            service,
            action="update_view",
            filter_view_id=31,
            sort_specs=[{"column": "Z"}],
        )


@pytest.mark.asyncio
async def test_update_view_with_nothing_to_change_is_refused():
    service = service_with(
        [
            {
                "properties": {"sheetId": 0, "title": "Data"},
                "filterViews": [{"filterViewId": 31, "range": GRID}],
            }
        ]
    )
    with pytest.raises(UserInputError, match="at least one of title, range_name"):
        await _call(service, action="update_view", filter_view_id=31)


@pytest.mark.asyncio
async def test_delete_view_needs_no_metadata_fetch():
    service = service_with(TWO_SHEETS)
    result = await _call(service, action="delete_view", filter_view_id=31)
    assert sent_request(service) == {"deleteFilterView": {"filterId": 31}}
    service.spreadsheets.return_value.get.assert_not_called()
    assert "deleted filter view 31" in result


@pytest.mark.asyncio
async def test_list_views_formats_basic_filter_and_views():
    service = service_with(
        [
            {
                "properties": {"sheetId": 0, "title": "Data"},
                "basicFilter": {"range": GRID, "sortSpecs": [{"dimensionIndex": 1}]},
                "filterViews": [
                    {
                        "filterViewId": 31,
                        "title": "Open",
                        "range": GRID,
                        "filterSpecs": [{"columnIndex": 1}, {"columnIndex": 3}],
                    }
                ],
            },
            {"properties": {"sheetId": 7, "title": "Summary"}},
        ]
    )
    result = await _call(service, action="list_views")
    assert (
        get_fields(service)
        == "sheets(properties(sheetId,title),basicFilter,filterViews)"
    )
    assert "Sheet 'Data':" in result
    assert "Basic filter on Data!A1:F200 (1 sort spec(s))" in result
    assert "Filter view 'Open' (ID 31) on Data!A1:F200 (2 filtered column(s))" in result
    assert "Summary" not in result
    service.spreadsheets.return_value.batchUpdate.assert_not_called()


@pytest.mark.asyncio
async def test_list_views_with_nothing_to_show():
    service = service_with(TWO_SHEETS)
    result = await _call(service, action="list_views")
    assert "No basic filters or filter views found." in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"action": "freeze"}, "Invalid action"),
        ({"action": "set_basic"}, "range_name is required"),
        ({"action": "create_view", "range_name": "A1:B2"}, "title is required"),
        ({"action": "delete_view"}, "filter_view_id is required"),
        (
            {
                "action": "set_basic",
                "range_name": "A1:F200",
                "filter_specs": [{"column": "B"}],
            },
            "needs hidden_values and/or condition_type",
        ),
        (
            {
                "action": "set_basic",
                "range_name": "A1:F200",
                "filter_specs": [{"column": "B", "condition_type": "NUMBER_BIGGER"}],
            },
            "not a valid condition type",
        ),
        (
            {
                "action": "set_basic",
                "range_name": "A1:F200",
                "sort_specs": [{"column": "B", "order": "sideways"}],
            },
            "order must be ASCENDING or DESCENDING",
        ),
    ],
)
async def test_invalid_input_is_refused(kwargs, message):
    service = service_with(TWO_SHEETS)
    with pytest.raises(UserInputError, match=message):
        await _call(service, **kwargs)
    service.spreadsheets.return_value.batchUpdate.assert_not_called()
