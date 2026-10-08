"""Tests for manage_sheet_validation."""

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

GRID = {
    "sheetId": 0,
    "startRowIndex": 1,
    "endRowIndex": 100,
    "startColumnIndex": 2,
    "endColumnIndex": 3,
}


async def _call(service, **kwargs):
    return await unwrap(sheets_tools.manage_sheet_validation)(
        service=service,
        user_google_email=EMAIL,
        spreadsheet_id=SPREADSHEET,
        range_name="Data!C2:C100",
        **kwargs,
    )


def _sent_rule(service):
    body = sent_request(service)["setDataValidation"]
    assert body["range"] == GRID
    return body.get("rule")


@pytest.mark.asyncio
async def test_list_rule_builds_one_of_list_dropdown():
    service = service_with(TWO_SHEETS)
    result = await _call(
        service,
        rule_type="list",
        values=["Open", "Closed", 3],
        input_message="Pick a status",
    )
    assert _sent_rule(service) == {
        "condition": {
            "type": "ONE_OF_LIST",
            "values": [
                {"userEnteredValue": "Open"},
                {"userEnteredValue": "Closed"},
                {"userEnteredValue": "3"},
            ],
        },
        "strict": True,
        "showCustomUi": True,
        "inputMessage": "Pick a status",
    }
    assert "set list validation (ONE_OF_LIST: Open, Closed, 3, strict)" in result


@pytest.mark.asyncio
async def test_list_rule_accepts_json_values_and_hides_dropdown():
    service = service_with(TWO_SHEETS)
    await _call(
        service,
        rule_type="list",
        values='["a", "b"]',
        show_dropdown=False,
        strict=False,
    )
    rule = _sent_rule(service)
    assert rule["showCustomUi"] is False
    assert rule["strict"] is False


@pytest.mark.asyncio
async def test_range_rule_prefixes_equals_sign():
    service = service_with(TWO_SHEETS)
    await _call(service, rule_type="range", values=["Lists!A2:A20"])
    assert _sent_rule(service)["condition"] == {
        "type": "ONE_OF_RANGE",
        "values": [{"userEnteredValue": "=Lists!A2:A20"}],
    }


@pytest.mark.asyncio
async def test_checkbox_rule_with_and_without_custom_values():
    service = service_with(TWO_SHEETS)
    await _call(service, rule_type="checkbox")
    rule = _sent_rule(service)
    assert rule["condition"] == {"type": "BOOLEAN"}
    assert "showCustomUi" not in rule

    service = service_with(TWO_SHEETS)
    await _call(service, rule_type="checkbox", values=["Yes", "No"])
    assert _sent_rule(service)["condition"]["values"] == [
        {"userEnteredValue": "Yes"},
        {"userEnteredValue": "No"},
    ]


@pytest.mark.asyncio
async def test_number_between_takes_two_operands():
    service = service_with(TWO_SHEETS)
    await _call(
        service, rule_type="number", condition_type="number_between", values=[1, 10]
    )
    assert _sent_rule(service)["condition"] == {
        "type": "NUMBER_BETWEEN",
        "values": [{"userEnteredValue": "1"}, {"userEnteredValue": "10"}],
    }


@pytest.mark.asyncio
async def test_date_rule_accepts_relative_dates():
    service = service_with(TWO_SHEETS)
    result = await _call(
        service, rule_type="date", condition_type="DATE_AFTER", values=["today"]
    )
    assert _sent_rule(service)["condition"] == {
        "type": "DATE_AFTER",
        "values": [{"relativeDate": "TODAY"}],
    }
    assert "DATE_AFTER: TODAY" in result


@pytest.mark.asyncio
async def test_text_is_email_takes_no_operands():
    service = service_with(TWO_SHEETS)
    await _call(service, rule_type="text", condition_type="TEXT_IS_EMAIL")
    assert _sent_rule(service)["condition"] == {"type": "TEXT_IS_EMAIL"}


@pytest.mark.asyncio
async def test_custom_formula_rule():
    service = service_with(TWO_SHEETS)
    await _call(service, rule_type="custom_formula", values=["=C2>0"])
    assert _sent_rule(service)["condition"] == {
        "type": "CUSTOM_FORMULA",
        "values": [{"userEnteredValue": "=C2>0"}],
    }


@pytest.mark.asyncio
async def test_clear_sends_range_without_rule():
    service = service_with(TWO_SHEETS)
    result = await _call(service, action="clear")
    assert sent_request(service) == {"setDataValidation": {"range": GRID}}
    assert "cleared data validation on range 'Data!C2:C100'" in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"action": "toggle"}, "Invalid action"),
        ({}, "rule_type must be one of"),
        ({"rule_type": "list"}, "at least one entry in values"),
        ({"rule_type": "range", "values": ["A1:A2", "B1:B2"]}, "exactly one value"),
        ({"rule_type": "checkbox", "values": ["Yes"]}, "exactly two"),
        (
            {"rule_type": "number", "values": [1]},
            "condition_type to be one of: NUMBER_BETWEEN",
        ),
        (
            {"rule_type": "number", "condition_type": "TEXT_EQ", "values": ["x"]},
            "condition_type to be one of",
        ),
        (
            {"rule_type": "number", "condition_type": "NUMBER_BETWEEN", "values": [1]},
            "exactly 2 value",
        ),
        ({"rule_type": "custom_formula", "values": ["C2>0"]}, "starting with '='"),
    ],
)
async def test_invalid_rules_are_refused(kwargs, message):
    service = service_with(TWO_SHEETS)
    with pytest.raises(UserInputError, match=message):
        await _call(service, **kwargs)
    service.spreadsheets.return_value.batchUpdate.assert_not_called()
