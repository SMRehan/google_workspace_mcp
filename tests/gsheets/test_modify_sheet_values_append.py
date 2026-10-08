"""Tests for modify_sheet_values(append=True)."""

import os
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from core.utils import UserInputError
from gsheets import sheets_tools
from tests.gsheets._batch_helpers import EMAIL, SPREADSHEET, unwrap


def _service(append_response=None):
    service = Mock()
    values_api = service.spreadsheets.return_value.values.return_value
    values_api.append.return_value.execute = Mock(
        return_value=append_response
        or {
            "spreadsheetId": SPREADSHEET,
            "tableRange": "Sheet1!A1:B3",
            "updates": {
                "updatedRange": "Sheet1!A4:B5",
                "updatedRows": 2,
                "updatedColumns": 2,
                "updatedCells": 4,
                "updatedData": {"values": [["x", "1"], ["y", "2"]]},
            },
        }
    )
    values_api.update.return_value.execute = Mock(return_value={"updatedCells": 0})
    return service, values_api


async def _call(service, **kwargs):
    return await unwrap(sheets_tools.modify_sheet_values)(
        service=service,
        user_google_email=EMAIL,
        spreadsheet_id=SPREADSHEET,
        range_name="Sheet1!A:B",
        **kwargs,
    )


@pytest.mark.asyncio
async def test_append_uses_values_append_with_same_value_input_option():
    service, values_api = _service()

    result = await _call(
        service, values=[["x", "1"], ["y", "2"]], append=True, value_input_option="RAW"
    )

    values_api.update.assert_not_called()
    kwargs = values_api.append.call_args.kwargs
    assert kwargs["spreadsheetId"] == SPREADSHEET
    assert kwargs["range"] == "Sheet1!A:B"
    assert kwargs["valueInputOption"] == "RAW"
    assert kwargs["body"] == {"values": [["x", "1"], ["y", "2"]]}
    assert "appended 2 rows" in result
    assert "Written to 'Sheet1!A4:B5'" in result
    assert "4 cells, 2 rows, 2 columns" in result


@pytest.mark.asyncio
async def test_append_accepts_json_string_values():
    service, values_api = _service()
    await _call(service, values='[["x", "1"]]', append=True)
    assert values_api.append.call_args.kwargs["body"] == {"values": [["x", "1"]]}


@pytest.mark.asyncio
async def test_append_reports_sheets_error_tokens_in_written_data():
    service, _ = _service(
        {
            "updates": {
                "updatedRange": "Sheet1!A4:A4",
                "updatedRows": 1,
                "updatedColumns": 1,
                "updatedCells": 1,
                "updatedData": {"values": [["#REF!"]]},
            }
        }
    )
    service.spreadsheets.return_value.get.return_value.execute = Mock(
        return_value={
            "sheets": [
                {
                    "properties": {"title": "Sheet1"},
                    "data": [
                        {
                            "startRow": 3,
                            "startColumn": 0,
                            "rowData": [
                                {
                                    "values": [
                                        {
                                            "formattedValue": "#REF!",
                                            "effectiveValue": {
                                                "errorValue": {
                                                    "type": "REF",
                                                    "message": "Reference does not exist.",
                                                }
                                            },
                                        }
                                    ]
                                }
                            ],
                        }
                    ],
                }
            ]
        }
    )

    result = await _call(service, values=[["=Nope!A1"]], append=True)

    assert "appended 1 rows" in result
    assert "Reference does not exist." in result


@pytest.mark.asyncio
async def test_default_write_still_uses_update():
    service, values_api = _service()
    values_api.update.return_value.execute = Mock(
        return_value={"updatedCells": 1, "updatedRows": 1, "updatedColumns": 1}
    )
    result = await _call(service, values=[["x"]])
    values_api.append.assert_not_called()
    assert "Successfully updated range" in result


@pytest.mark.asyncio
async def test_append_with_clear_values_is_refused():
    service, values_api = _service()
    with pytest.raises(
        UserInputError, match="append=True cannot be combined with clear_values"
    ):
        await _call(service, append=True, clear_values=True)
    values_api.append.assert_not_called()


@pytest.mark.asyncio
async def test_append_with_chips_is_refused():
    service, values_api = _service()
    with pytest.raises(UserInputError, match="cannot be combined with chips"):
        await _call(service, append=True, chips="user@example.com")
    values_api.append.assert_not_called()


@pytest.mark.asyncio
async def test_append_without_values_is_refused():
    service, _ = _service()
    with pytest.raises(UserInputError, match="'values' or 'chips'"):
        await _call(service, append=True)
