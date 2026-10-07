"""Unit tests for get_drive_about."""

import os
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from gdrive.drive_helpers import ABOUT_FIELDS, _format_bytes, _get_drive_about_impl
from gdrive.drive_tools import get_drive_about

EMAIL = "user@example.com"


def _unwrap(tool):
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _about_service(payload):
    service = Mock()
    service.about().get().execute.return_value = payload
    service.about.reset_mock()
    return service


_ABOUT = {
    "user": {"displayName": "Ada", "emailAddress": EMAIL, "permissionId": "p-1"},
    "storageQuota": {
        "limit": str(15 * 1024**3),
        "usage": str(3 * 1024**3),
        "usageInDrive": str(2 * 1024**3),
        "usageInDriveTrash": str(512 * 1024**2),
    },
    "maxUploadSize": "5242880000000",
    "canCreateDrives": True,
    "importFormats": {"text/csv": ["application/vnd.google-apps.spreadsheet"]},
    "exportFormats": {
        "application/vnd.google-apps.document": ["application/pdf"],
        "application/vnd.google-apps.spreadsheet": ["text/csv"],
    },
}


@pytest.mark.asyncio
async def test_about_requests_explicit_fields_and_summarises():
    service = _about_service(_ABOUT)

    result = await _get_drive_about_impl(service, EMAIL)

    service.about.return_value.get.assert_called_once_with(fields=ABOUT_FIELDS)
    assert "User: Ada <user@example.com> (permission ID: p-1)" in result
    assert "Total used: 3.0 GB" in result
    assert "Limit: 15.0 GB" in result
    assert "Used: 20.0%" in result
    assert "Used in Drive: 2.0 GB" in result
    assert "Used in trash: 512.0 MB" in result
    assert "Max upload size: 4.8 TB" in result
    assert "Can create shared drives: yes" in result
    assert "Import conversions: 1 source types" in result
    assert "Export conversions: 2 Google types" in result


@pytest.mark.asyncio
async def test_about_without_limit_reports_unlimited():
    payload = {**_ABOUT, "storageQuota": {"usage": "1024"}, "canCreateDrives": False}
    service = _about_service(payload)

    result = await _get_drive_about_impl(service, EMAIL)

    assert "Limit: unlimited" in result
    assert "Used:" not in result.split("Limit: unlimited")[1].split("\n")[1]
    assert "Can create shared drives: no" in result


@pytest.mark.asyncio
async def test_about_tolerates_missing_sections():
    service = _about_service({})

    result = await _get_drive_about_impl(service, EMAIL)

    assert "User: Unknown <unknown>" in result
    assert "Total used: unknown" in result
    assert "Max upload size: unknown" in result


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "unknown"),
        ("abc", "unknown"),
        (0, "0 B"),
        (1023, "1,023 B"),
        (2048, "2.0 KB (2,048 bytes)"),
        (5 * 1024**2, "5.0 MB (5,242,880 bytes)"),
    ],
)
def test_format_bytes(value, expected):
    assert _format_bytes(value) == expected


@pytest.mark.asyncio
async def test_tool_delegates_to_helper():
    service = _about_service(_ABOUT)

    result = await _unwrap(get_drive_about)(service=service, user_google_email=EMAIL)

    assert result.startswith(f"Drive account for {EMAIL}:")
