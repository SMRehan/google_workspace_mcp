"""Tests for read_sheet_values(ranges=[...]) multi-range reads via values.batchGet."""

import os
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from core.utils import UserInputError
from gsheets import sheets_tools
from tests.gsheets._batch_helpers import EMAIL, SPREADSHEET, unwrap


def _service(value_ranges):
    service = Mock()
    values_api = service.spreadsheets.return_value.values.return_value
    values_api.batchGet.return_value.execute = Mock(
        return_value={"spreadsheetId": SPREADSHEET, "valueRanges": value_ranges}
    )
    return service, values_api


async def _call(service, **kwargs):
    return await unwrap(sheets_tools.read_sheet_values)(
        service=service,
        user_google_email=EMAIL,
        spreadsheet_id=SPREADSHEET,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_ranges_use_batch_get_and_format_each_block():
    service, values_api = _service(
        [
            {"range": "Data!A1:B2", "values": [["h1", "h2"], ["1"]]},
            {"range": "Summary!A1:A1"},
        ]
    )

    result = await _call(service, ranges=["Data!A1:B2", "Summary!A1"])

    values_api.get.assert_not_called()
    values_api.batchGet.assert_called_once_with(
        spreadsheetId=SPREADSHEET, ranges=["Data!A1:B2", "Summary!A1"]
    )
    assert "Successfully read 2 ranges (2 rows total)" in result
    assert "Range 1 'Data!A1:B2': 2 rows" in result
    assert "Row  1: ['h1', 'h2']" in result
    assert "Row  2: ['1', '']" in result  # padded to the first row's width
    assert (
        "Range 2 'Summary!A1' (resolved to 'Summary!A1:A1'): no data found." in result
    )


@pytest.mark.asyncio
async def test_ranges_are_clamped_before_the_api_call():
    service, values_api = _service([{"range": "Data!A1:A1000", "values": [["1"]]}])

    result = await _call(service, ranges=["Data!A:A"])

    assert values_api.batchGet.call_args.kwargs["ranges"] == ["Data!A1:A1000"]
    assert "clamped to 'Data!A1:A1000'" in result


@pytest.mark.asyncio
async def test_ranges_accepts_json_string():
    service, values_api = _service([{"range": "Data!A1:A1", "values": [["1"]]}])
    await _call(service, ranges='["Data!A1"]')
    assert values_api.batchGet.call_args.kwargs["ranges"] == ["Data!A1"]


@pytest.mark.asyncio
async def test_ranges_with_metadata_flags_is_refused():
    service, values_api = _service([])
    with pytest.raises(UserInputError, match="not supported with `ranges`"):
        await _call(service, ranges=["Data!A1"], include_formulas=True)
    values_api.batchGet.assert_not_called()


@pytest.mark.asyncio
async def test_empty_ranges_is_refused():
    service, values_api = _service([])
    with pytest.raises(UserInputError, match="at least one A1 range"):
        await _call(service, ranges=[])
    values_api.batchGet.assert_not_called()
