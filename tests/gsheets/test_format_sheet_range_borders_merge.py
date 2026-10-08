"""Tests for format_sheet_range borders, merge and unmerge."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from core.utils import UserInputError
from gsheets import sheets_tools
from gsheets.sheets_helpers import _normalize_border_sides
from tests.gsheets._batch_helpers import (
    EMAIL,
    SPREADSHEET,
    TWO_SHEETS,
    sent_requests,
    service_with,
    unwrap,
)

GRID = {
    "sheetId": 0,
    "startRowIndex": 0,
    "endRowIndex": 3,
    "startColumnIndex": 0,
    "endColumnIndex": 2,
}


async def _call(service, **kwargs):
    return await unwrap(sheets_tools.format_sheet_range)(
        service=service,
        user_google_email=EMAIL,
        spreadsheet_id=SPREADSHEET,
        range_name="Data!A1:B3",
        **kwargs,
    )


@pytest.mark.asyncio
async def test_border_style_alone_sends_update_borders_on_all_sides():
    service = service_with(TWO_SHEETS)

    result = await _call(service, border_style="solid", border_color="#FF0000")

    requests = sent_requests(service)
    assert len(requests) == 1
    borders = requests[0]["updateBorders"]
    assert borders["range"] == GRID
    for side in ("top", "bottom", "left", "right", "innerHorizontal", "innerVertical"):
        assert borders[side] == {
            "style": "SOLID",
            "color": {"red": 1.0, "green": 0.0, "blue": 0.0},
        }
    assert (
        "SOLID borders #FF0000 on top, bottom, left, right, innerHorizontal, innerVertical"
        in result
    )


@pytest.mark.asyncio
async def test_border_sides_outer_group_and_named_sides():
    service = service_with(TWO_SHEETS)
    await _call(service, border_style="DASHED", border_sides=["outer"])
    borders = sent_requests(service)[0]["updateBorders"]
    assert set(borders) == {"range", "top", "bottom", "left", "right"}
    assert borders["top"] == {"style": "DASHED"}

    service = service_with(TWO_SHEETS)
    await _call(
        service, border_style="DOUBLE", border_sides=["bottom", "inner_vertical"]
    )
    borders = sent_requests(service)[0]["updateBorders"]
    assert set(borders) == {"range", "bottom", "innerVertical"}


def test_normalize_border_sides_accepts_csv_string_and_dedupes():
    assert _normalize_border_sides("top, outer") == ["top", "bottom", "left", "right"]
    assert _normalize_border_sides(None) == [
        "top",
        "bottom",
        "left",
        "right",
        "innerHorizontal",
        "innerVertical",
    ]
    with pytest.raises(UserInputError, match="Unknown border side 'diagonal'"):
        _normalize_border_sides(["diagonal"])


@pytest.mark.asyncio
async def test_cell_format_is_sent_before_borders_and_merge():
    service = service_with(TWO_SHEETS)

    result = await _call(service, bold=True, border_style="SOLID_THICK", merge="all")

    requests = sent_requests(service)
    assert [next(iter(r)) for r in requests] == [
        "repeatCell",
        "updateBorders",
        "mergeCells",
    ]
    assert requests[2]["mergeCells"] == {"range": GRID, "mergeType": "MERGE_ALL"}
    assert "bold" in result and "merged all" in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "merge, merge_type",
    [("columns", "MERGE_COLUMNS"), ("rows", "MERGE_ROWS"), ("MERGE_ALL", "MERGE_ALL")],
)
async def test_merge_types(merge, merge_type):
    service = service_with(TWO_SHEETS)
    await _call(service, merge=merge)
    assert sent_requests(service) == [
        {"mergeCells": {"range": GRID, "mergeType": merge_type}}
    ]


@pytest.mark.asyncio
async def test_unmerge_sends_unmerge_cells():
    service = service_with(TWO_SHEETS)
    result = await _call(service, unmerge=True)
    assert sent_requests(service) == [{"unmergeCells": {"range": GRID}}]
    assert "unmerged cells" in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"merge": "all", "unmerge": True}, "either merge or unmerge"),
        ({"merge": "diagonal"}, "merge must be one of"),
        ({"border_style": "WAVY"}, "border_style must be one of"),
        ({"border_color": "#000000"}, "border_style is required"),
        ({}, "at least one formatting option"),
    ],
)
async def test_invalid_border_and_merge_input_is_refused(kwargs, message):
    service = service_with(TWO_SHEETS)
    with pytest.raises(UserInputError, match=message):
        await _call(service, **kwargs)
    service.spreadsheets.return_value.batchUpdate.assert_not_called()
