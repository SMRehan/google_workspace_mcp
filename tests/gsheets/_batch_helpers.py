"""Shared Mock-service builders for the batchUpdate-backed Sheets tool tests."""

from unittest.mock import Mock

EMAIL = "user@example.com"
SPREADSHEET = "sheet123"

TWO_SHEETS = [
    {"properties": {"sheetId": 0, "title": "Data", "index": 0}},
    {"properties": {"sheetId": 7, "title": "Summary", "index": 1}},
]


def unwrap(tool):
    """Unwrap FunctionTool + decorators to the original async function."""
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def service_with(sheets=None, replies=None, get_response=None):
    """Sheets service whose spreadsheets().get returns `sheets` and batchUpdate `replies`."""
    service = Mock()
    get_payload = get_response if get_response is not None else {"sheets": sheets or []}
    service.spreadsheets.return_value.get.return_value.execute = Mock(
        return_value=get_payload
    )
    service.spreadsheets.return_value.batchUpdate.return_value.execute = Mock(
        return_value={"replies": replies if replies is not None else [{}]}
    )
    return service


def sent_requests(service):
    body = service.spreadsheets.return_value.batchUpdate.call_args.kwargs["body"]
    return body["requests"]


def sent_request(service):
    requests = sent_requests(service)
    assert len(requests) == 1
    return requests[0]


def get_fields(service):
    return service.spreadsheets.return_value.get.call_args.kwargs.get("fields")
