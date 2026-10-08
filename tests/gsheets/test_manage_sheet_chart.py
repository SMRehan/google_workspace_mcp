"""Tests for manage_sheet_chart."""

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


def _grid(start_col, end_col, rows=13):
    return {
        "sheetId": 0,
        "startRowIndex": 0,
        "endRowIndex": rows,
        "startColumnIndex": start_col,
        "endColumnIndex": end_col,
    }


def _source(grid):
    return {"sourceRange": {"sources": [grid]}}


EXISTING_CHART = {
    "chartId": 9,
    "spec": {
        "title": "Revenue",
        "basicChart": {"chartType": "COLUMN", "legendPosition": "BOTTOM_LEGEND"},
    },
    "position": {
        "overlayPosition": {
            "anchorCell": {"sheetId": 0, "rowIndex": 1, "columnIndex": 4},
            "widthPixels": 600,
            "heightPixels": 371,
        }
    },
}
PIE_CHART = {
    "chartId": 10,
    "spec": {"title": "Share", "pieChart": {"legendPosition": "RIGHT_LEGEND"}},
    "position": {"sheetId": 7},
}
SHEETS_WITH_CHARTS = [
    {"properties": {"sheetId": 0, "title": "Data"}, "charts": [EXISTING_CHART]},
    {"properties": {"sheetId": 7, "title": "Summary"}, "charts": [PIE_CHART]},
]


async def _call(service, **kwargs):
    return await unwrap(sheets_tools.manage_sheet_chart)(
        service=service,
        user_google_email=EMAIL,
        spreadsheet_id=SPREADSHEET,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_add_column_chart_anchored_with_defaults():
    service = service_with(
        TWO_SHEETS, replies=[{"addChart": {"chart": {"chartId": 12}}}]
    )
    result = await _call(
        service,
        action="add",
        chart_type="column",
        title="Revenue",
        domain_range="Data!A1:A13",
        series_ranges=["Data!B1:B13", "Data!C1:C13"],
        anchor_cell="Data!E2",
        header_count=1,
        legend_position="bottom",
    )
    chart = sent_request(service)["addChart"]["chart"]
    assert chart["spec"] == {
        "title": "Revenue",
        "basicChart": {
            "chartType": "COLUMN",
            "domains": [{"domain": _source(_grid(0, 1))}],
            "series": [
                {"series": _source(_grid(1, 2)), "targetAxis": "LEFT_AXIS"},
                {"series": _source(_grid(2, 3)), "targetAxis": "LEFT_AXIS"},
            ],
            "legendPosition": "BOTTOM_LEGEND",
            "headerCount": 1,
        },
    }
    assert chart["position"] == {
        "overlayPosition": {
            "anchorCell": {"sheetId": 0, "rowIndex": 1, "columnIndex": 4},
            "widthPixels": 600,
            "heightPixels": 371,
        }
    }
    assert "added COLUMN chart 'Revenue' (ID: 12)" in result


@pytest.mark.asyncio
async def test_add_bar_chart_targets_bottom_axis_and_new_sheet_without_anchor():
    service = service_with(
        TWO_SHEETS, replies=[{"addChart": {"chart": {"chartId": 13}}}]
    )
    result = await _call(
        service,
        action="add",
        chart_type="BAR",
        domain_range="A1:A13",
        series_ranges='["B1:B13"]',
        width=400,
        height=300,
    )
    chart = sent_request(service)["addChart"]["chart"]
    assert chart["spec"]["basicChart"]["series"][0]["targetAxis"] == "BOTTOM_AXIS"
    assert "headerCount" not in chart["spec"]["basicChart"]
    assert chart["position"] == {"newSheet": True}
    assert "on a new sheet" in result


@pytest.mark.asyncio
async def test_add_pie_chart_uses_pie_spec():
    service = service_with(TWO_SHEETS)
    await _call(
        service,
        action="add",
        chart_type="PIE",
        title="Share",
        subtitle="FY26",
        domain_range="Data!A2:A6",
        series_ranges=["Data!B2:B6"],
        anchor_cell="Summary!A1",
        legend_position="LABELED_LEGEND",
    )
    spec = sent_request(service)["addChart"]["chart"]["spec"]
    assert spec["subtitle"] == "FY26"
    assert spec["pieChart"] == {
        "domain": _source(
            {
                "sheetId": 0,
                "startRowIndex": 1,
                "endRowIndex": 6,
                "startColumnIndex": 0,
                "endColumnIndex": 1,
            }
        ),
        "series": _source(
            {
                "sheetId": 0,
                "startRowIndex": 1,
                "endRowIndex": 6,
                "startColumnIndex": 1,
                "endColumnIndex": 2,
            }
        ),
        "legendPosition": "LABELED_LEGEND",
    }
    assert "basicChart" not in spec


@pytest.mark.asyncio
async def test_update_patches_the_fetched_spec():
    service = service_with(SHEETS_WITH_CHARTS)
    result = await _call(
        service,
        action="update",
        chart_id=9,
        title="Revenue FY26",
        chart_type="line",
        legend_position="none",
    )
    assert get_fields(service) == "sheets(properties(sheetId,title),charts)"
    sent = sent_request(service)["updateChartSpec"]
    assert sent["chartId"] == 9
    assert sent["spec"] == {
        "title": "Revenue FY26",
        "basicChart": {"chartType": "LINE", "legendPosition": "NO_LEGEND"},
    }
    # The fetched chart must not be mutated in place.
    assert EXISTING_CHART["spec"]["title"] == "Revenue"
    assert "updated chart 9" in result


@pytest.mark.asyncio
async def test_update_refuses_pie_conversion_and_unknown_chart():
    service = service_with(SHEETS_WITH_CHARTS)
    with pytest.raises(UserInputError, match="to or from PIE"):
        await _call(service, action="update", chart_id=9, chart_type="PIE")
    with pytest.raises(UserInputError, match="to or from PIE"):
        await _call(service, action="update", chart_id=10, chart_type="BAR")
    with pytest.raises(UserInputError, match="No chart with chart_id 99"):
        await _call(service, action="update", chart_id=99, title="x")
    with pytest.raises(UserInputError, match="at least one of title"):
        await _call(service, action="update", chart_id=9)
    service.spreadsheets.return_value.batchUpdate.assert_not_called()


@pytest.mark.asyncio
async def test_move_sets_only_supplied_overlay_fields():
    service = service_with(TWO_SHEETS)
    await _call(service, action="move", chart_id=9, anchor_cell="Summary!B3")
    assert sent_request(service) == {
        "updateEmbeddedObjectPosition": {
            "objectId": 9,
            "newPosition": {
                "overlayPosition": {
                    "anchorCell": {"sheetId": 7, "rowIndex": 2, "columnIndex": 1}
                }
            },
            "fields": "anchorCell",
        }
    }

    service = service_with(TWO_SHEETS)
    result = await _call(service, action="move", chart_id=9, width=800, height=500)
    sent = sent_request(service)["updateEmbeddedObjectPosition"]
    assert sent["newPosition"] == {
        "overlayPosition": {"widthPixels": 800, "heightPixels": 500}
    }
    assert sent["fields"] == "widthPixels,heightPixels"
    service.spreadsheets.return_value.get.assert_not_called()
    assert "moved chart 9 (800x500px)" in result


@pytest.mark.asyncio
async def test_delete_sends_delete_embedded_object():
    service = service_with(TWO_SHEETS)
    result = await _call(service, action="delete", chart_id=9)
    assert sent_request(service) == {"deleteEmbeddedObject": {"objectId": 9}}
    assert "deleted chart 9" in result


@pytest.mark.asyncio
async def test_list_describes_type_and_placement():
    service = service_with(SHEETS_WITH_CHARTS)
    result = await _call(service, action="list")
    assert "Chart 9: 'Revenue' [COLUMN] anchored at Data!E2, 600x371px" in result
    assert "Chart 10: 'Share' [PIE] own sheet 'Summary'" in result
    service.spreadsheets.return_value.batchUpdate.assert_not_called()

    assert "No charts found." in await _call(service_with(TWO_SHEETS), action="list")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"action": "render"}, "Invalid action"),
        ({"action": "delete"}, "chart_id is required"),
        (
            {"action": "add", "domain_range": "A1:A2", "series_ranges": ["B1:B2"]},
            "chart_type is required",
        ),
        (
            {"action": "add", "chart_type": "LINE", "series_ranges": ["B1:B2"]},
            "domain_range is required",
        ),
        (
            {"action": "add", "chart_type": "LINE", "domain_range": "A1:A2"},
            "series_ranges is required",
        ),
        (
            {
                "action": "add",
                "chart_type": "DONUT",
                "domain_range": "A1:A2",
                "series_ranges": ["B1:B2"],
            },
            "chart_type must be one of",
        ),
        (
            {
                "action": "add",
                "chart_type": "PIE",
                "domain_range": "A1:A2",
                "series_ranges": ["B1:B2", "C1:C2"],
            },
            "exactly one series",
        ),
        (
            {
                "action": "add",
                "chart_type": "LINE",
                "domain_range": "A1:A2",
                "series_ranges": ["B1:B2"],
                "anchor_cell": "E2:F3",
            },
            "single cell",
        ),
        (
            {
                "action": "add",
                "chart_type": "LINE",
                "domain_range": "A1:A2",
                "series_ranges": ["B1:B2"],
                "anchor_cell": "E",
            },
            "both a column and a row",
        ),
        (
            {
                "action": "add",
                "chart_type": "LINE",
                "domain_range": "A1:A2",
                "series_ranges": ["B1:B2"],
                "anchor_cell": "E2",
                "width": 0,
            },
            "positive number of pixels",
        ),
        (
            {
                "action": "add",
                "chart_type": "LINE",
                "domain_range": "A1:A2",
                "series_ranges": ["B1:B2"],
                "legend_position": "middle",
            },
            "legend_position must be one of",
        ),
        ({"action": "move", "chart_id": 9}, "needs anchor_cell and/or width/height"),
    ],
)
async def test_invalid_input_is_refused(kwargs, message):
    service = service_with(TWO_SHEETS)
    with pytest.raises(UserInputError, match=message):
        await _call(service, **kwargs)
    service.spreadsheets.return_value.batchUpdate.assert_not_called()
