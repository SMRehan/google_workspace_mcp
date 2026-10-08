"""
Unit tests for the "Other contacts", Workspace directory and contact photo tools.

Every People API call goes through a Mock service; the photo URL download is
exercised through a fake SSRF-safe stream so nothing touches the network.
"""

import base64
import os
import sys
from contextlib import asynccontextmanager
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from core.utils import UserInputError
from gcontacts import contacts_helpers
from gcontacts.contacts_helpers import (
    DIRECTORY_READ_MASK,
    MAX_CONTACT_PHOTO_BYTES,
    OTHER_CONTACTS_READ_MASK,
    _decode_photo_base64,
    _format_person_page,
    _normalize_directory_sources,
    _normalize_read_mask,
    _normalize_resource_name,
)
from gcontacts.contacts_tools import (
    DETAILED_PERSON_FIELDS,
    list_other_contacts,
    manage_contact,
    manage_contact_photo,
    search_directory,
)

EMAIL = "user@example.com"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _unwrap(tool):
    """Unwrap FunctionTool + decorators to the original async function."""
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _person(resource="otherContacts/c1", name="Ada Lovelace", email="ada@x.io"):
    return {
        "resourceName": resource,
        "names": [{"displayName": name}],
        "emailAddresses": [{"value": email}],
    }


# =============================================================================
# Helpers
# =============================================================================


class TestHelpers:
    def test_normalize_resource_name_adds_prefix(self):
        assert _normalize_resource_name("c1") == "people/c1"
        assert _normalize_resource_name("c1", "otherContacts") == "otherContacts/c1"

    def test_normalize_resource_name_keeps_existing_collection(self):
        assert _normalize_resource_name("otherContacts/c1") == "otherContacts/c1"
        assert _normalize_resource_name(" people/c2 ") == "people/c2"

    def test_normalize_resource_name_rejects_empty(self):
        with pytest.raises(UserInputError):
            _normalize_resource_name("   ")

    def test_normalize_read_mask_default_and_trim(self):
        assert _normalize_read_mask(None, "a,b") == "a,b"
        assert _normalize_read_mask(" , ", "a,b") == "a,b"
        assert _normalize_read_mask(" names , photos ", "a") == "names,photos"

    def test_directory_sources_default_is_both(self):
        assert _normalize_directory_sources(None) == [
            "DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE",
            "DIRECTORY_SOURCE_TYPE_DOMAIN_CONTACT",
        ]

    def test_directory_sources_accepts_short_and_full_names(self):
        assert _normalize_directory_sources(
            ["domain_contact", "DIRECTORY_SOURCE_TYPE_DOMAIN_CONTACT"]
        ) == ["DIRECTORY_SOURCE_TYPE_DOMAIN_CONTACT"]

    def test_directory_sources_rejects_unknown(self):
        with pytest.raises(UserInputError, match="Invalid directory source"):
            _normalize_directory_sources(["PROFILE"])

    def test_format_person_page_empty(self):
        assert _format_person_page([], "H", "nothing") == "nothing"

    def test_format_person_page_counts_and_token(self):
        out = _format_person_page(
            [_person()], "Heading", "nothing", next_page_token="tok", total_size=7
        )
        assert out.startswith("Heading (1 of 7):")
        assert "Contact ID: otherContacts/c1" in out
        assert "Name: Ada Lovelace" in out
        assert out.endswith("Next page token: tok")

    def test_format_person_page_without_total(self):
        out = _format_person_page([_person(), _person("people/c2")], "H", "n")
        assert out.startswith("H (2):")
        assert "Next page token" not in out

    def test_decode_photo_base64_plain_and_data_url(self):
        encoded = base64.b64encode(PNG_BYTES).decode()
        assert _decode_photo_base64(encoded) == PNG_BYTES
        assert _decode_photo_base64(f"data:image/png;base64,{encoded}") == PNG_BYTES
        assert _decode_photo_base64(encoded[:10] + "\n " + encoded[10:]) == PNG_BYTES

    @pytest.mark.parametrize(
        "bad",
        ["", "   ", "not*base64!", "data:image/png,abcd", "data:image/png;base64"],
    )
    def test_decode_photo_base64_rejects_malformed(self, bad):
        with pytest.raises(UserInputError):
            _decode_photo_base64(bad)

    def test_decode_photo_base64_enforces_size_cap(self):
        encoded = base64.b64encode(b"x" * (MAX_CONTACT_PHOTO_BYTES + 1)).decode()
        with pytest.raises(UserInputError, match="limit"):
            _decode_photo_base64(encoded)


# =============================================================================
# list_other_contacts
# =============================================================================


def _other_contacts_service(list_response=None, search_response=None):
    service = Mock()
    service.otherContacts().list().execute = Mock(return_value=list_response or {})
    service.otherContacts().search().execute = Mock(return_value=search_response or {})
    return service


class TestListOtherContacts:
    @pytest.mark.asyncio
    async def test_list_forwards_defaults_and_surfaces_paging(self):
        service = _other_contacts_service(
            list_response={
                "otherContacts": [_person()],
                "nextPageToken": "next-1",
                "totalSize": 42,
            }
        )
        out = await _unwrap(list_other_contacts)(
            service=service, user_google_email=EMAIL
        )
        assert service.otherContacts().list.call_args.kwargs == {
            "readMask": OTHER_CONTACTS_READ_MASK,
            "pageSize": 100,
        }
        assert "Other contacts for user@example.com (1 of 42):" in out
        # The otherContacts/ prefix is kept: manage_contact copy_to_my_contacts needs it.
        assert "Contact ID: otherContacts/c1" in out
        assert "Next page token: next-1" in out

    @pytest.mark.asyncio
    async def test_list_forwards_page_token_mask_and_caps_page_size(self):
        service = _other_contacts_service(list_response={"otherContacts": []})
        out = await _unwrap(list_other_contacts)(
            service=service,
            user_google_email=EMAIL,
            action="list",
            page_size=5000,
            page_token="tok",
            read_mask=" names, photos ",
        )
        assert service.otherContacts().list.call_args.kwargs == {
            "readMask": "names,photos",
            "pageSize": 1000,
            "pageToken": "tok",
        }
        assert out == "No other contacts found for user@example.com."

    @pytest.mark.asyncio
    async def test_search_unwraps_results_and_caps_at_30(self):
        service = _other_contacts_service(
            search_response={
                "results": [
                    {"person": _person()},
                    {"person": _person("otherContacts/c2")},
                ]
            }
        )
        out = await _unwrap(list_other_contacts)(
            service=service,
            user_google_email=EMAIL,
            action="search",
            query="ada",
            page_size=99,
        )
        assert service.otherContacts().search.call_args.kwargs == {
            "query": "ada",
            "readMask": OTHER_CONTACTS_READ_MASK,
            "pageSize": 30,
        }
        assert "Other contacts matching 'ada'" in out
        assert "Contact ID: otherContacts/c1" in out
        assert "Contact ID: otherContacts/c2" in out
        service.otherContacts().list().execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_search_without_query_is_rejected(self):
        with pytest.raises(UserInputError, match="query is required"):
            await _unwrap(list_other_contacts)(
                service=_other_contacts_service(),
                user_google_email=EMAIL,
                action="search",
            )

    @pytest.mark.asyncio
    async def test_search_no_matches(self):
        service = _other_contacts_service(search_response={})
        out = await _unwrap(list_other_contacts)(
            service=service, user_google_email=EMAIL, action="search", query="zz"
        )
        assert out == "No other contacts found matching 'zz' for user@example.com."

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kwargs", [{"action": "copy"}, {"page_size": 0}])
    async def test_invalid_input(self, kwargs):
        with pytest.raises(UserInputError):
            await _unwrap(list_other_contacts)(
                service=_other_contacts_service(), user_google_email=EMAIL, **kwargs
            )


# =============================================================================
# manage_contact action="copy_to_my_contacts"
# =============================================================================


class TestCopyToMyContacts:
    @pytest.mark.asyncio
    async def test_copy_calls_api_with_other_contact_resource(self):
        service = Mock()
        service.otherContacts().copyOtherContactToMyContactsGroup().execute = Mock(
            return_value=_person("people/c9", "Ada Lovelace")
        )
        out = await _unwrap(manage_contact)(
            service=service,
            user_google_email=EMAIL,
            action="copy_to_my_contacts",
            contact_id="otherContacts/c1",
        )
        kwargs = (
            service.otherContacts().copyOtherContactToMyContactsGroup.call_args.kwargs
        )
        assert kwargs == {
            "resourceName": "otherContacts/c1",
            "body": {"readMask": DETAILED_PERSON_FIELDS},
        }
        assert "copied to My Contacts" in out
        assert "Contact ID: c9" in out

    @pytest.mark.asyncio
    async def test_copy_normalizes_bare_id(self):
        service = Mock()
        service.otherContacts().copyOtherContactToMyContactsGroup().execute = Mock(
            return_value={}
        )
        await _unwrap(manage_contact)(
            service=service,
            user_google_email=EMAIL,
            action="copy_to_my_contacts",
            contact_id="c1",
        )
        kwargs = (
            service.otherContacts().copyOtherContactToMyContactsGroup.call_args.kwargs
        )
        assert kwargs["resourceName"] == "otherContacts/c1"

    @pytest.mark.asyncio
    async def test_copy_rejects_my_contacts_id(self):
        with pytest.raises(UserInputError, match="otherContacts/"):
            await _unwrap(manage_contact)(
                service=Mock(),
                user_google_email=EMAIL,
                action="copy_to_my_contacts",
                contact_id="people/c1",
            )

    @pytest.mark.asyncio
    async def test_copy_requires_contact_id(self):
        with pytest.raises(UserInputError, match="contact_id is required"):
            await _unwrap(manage_contact)(
                service=Mock(), user_google_email=EMAIL, action="copy_to_my_contacts"
            )


# =============================================================================
# search_directory
# =============================================================================


def _directory_service(search_response=None, list_response=None):
    service = Mock()
    service.people().searchDirectoryPeople().execute = Mock(
        return_value=search_response or {}
    )
    service.people().listDirectoryPeople().execute = Mock(
        return_value=list_response or {}
    )
    return service


class TestSearchDirectory:
    @pytest.mark.asyncio
    async def test_search_defaults(self):
        service = _directory_service(
            search_response={
                "people": [_person("people/d1", "Grace Hopper", "grace@corp.example")],
                "nextPageToken": "dir-2",
                "totalSize": 3,
            }
        )
        out = await _unwrap(search_directory)(
            service=service, user_google_email=EMAIL, query="gra"
        )
        assert service.people().searchDirectoryPeople.call_args.kwargs == {
            "readMask": DIRECTORY_READ_MASK,
            "sources": [
                "DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE",
                "DIRECTORY_SOURCE_TYPE_DOMAIN_CONTACT",
            ],
            "query": "gra",
            "pageSize": 100,
        }
        assert "Directory people matching 'gra' for user@example.com (1 of 3):" in out
        assert "Name: Grace Hopper" in out
        assert "Next page token: dir-2" in out
        service.people().listDirectoryPeople().execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_search_forwards_sources_mask_token_and_caps_500(self):
        service = _directory_service(search_response={"people": []})
        out = await _unwrap(search_directory)(
            service=service,
            user_google_email=EMAIL,
            action="search",
            query="x",
            sources=["domain_profile"],
            read_mask="names",
            page_size=9999,
            page_token="p2",
        )
        assert service.people().searchDirectoryPeople.call_args.kwargs == {
            "readMask": "names",
            "sources": ["DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE"],
            "pageToken": "p2",
            "query": "x",
            "pageSize": 500,
        }
        assert out == "No directory people found matching 'x' for user@example.com."

    @pytest.mark.asyncio
    async def test_list_uses_list_endpoint_and_caps_1000(self):
        service = _directory_service(
            list_response={
                "people": [_person("people/d1", "Grace Hopper")],
                "nextPageToken": "l2",
            }
        )
        out = await _unwrap(search_directory)(
            service=service,
            user_google_email=EMAIL,
            action="list",
            sources=["DIRECTORY_SOURCE_TYPE_DOMAIN_CONTACT"],
            page_size=5000,
        )
        assert service.people().listDirectoryPeople.call_args.kwargs == {
            "readMask": DIRECTORY_READ_MASK,
            "sources": ["DIRECTORY_SOURCE_TYPE_DOMAIN_CONTACT"],
            "pageSize": 1000,
        }
        assert "Directory people for user@example.com (1):" in out
        assert "Next page token: l2" in out
        service.people().searchDirectoryPeople().execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_list_empty(self):
        out = await _unwrap(search_directory)(
            service=_directory_service(), user_google_email=EMAIL, action="list"
        )
        assert out == "No directory people found for user@example.com."

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"action": "search"},
            {"action": "search", "query": "  "},
            {"action": "get", "query": "x"},
            {"query": "x", "page_size": 0},
            {"query": "x", "sources": ["ALL"]},
        ],
    )
    async def test_invalid_input(self, kwargs):
        with pytest.raises(UserInputError):
            await _unwrap(search_directory)(
                service=_directory_service(), user_google_email=EMAIL, **kwargs
            )


# =============================================================================
# manage_contact_photo
# =============================================================================


def _photo_service(update_response=None, delete_response=None):
    service = Mock()
    service.people().updateContactPhoto().execute = Mock(
        return_value=update_response or {}
    )
    service.people().deleteContactPhoto().execute = Mock(
        return_value=delete_response or {}
    )
    return service


class _FakeResponse:
    def __init__(self, body=PNG_BYTES, status=200, headers=None, chunk=16):
        self.status_code = status
        self.headers = headers if headers is not None else {"Content-Type": "image/png"}
        self._body = body
        self._chunk = chunk

    async def aiter_bytes(self, chunk_size=None):
        for i in range(0, len(self._body), self._chunk):
            yield self._body[i : i + self._chunk]


def _fake_stream(response, seen):
    @asynccontextmanager
    async def fake(url, **kwargs):
        seen.append(url)
        yield response

    return fake


class TestManageContactPhoto:
    @pytest.mark.asyncio
    async def test_update_from_base64_sends_photo_bytes(self):
        service = _photo_service(
            update_response={
                "person": {
                    **_person("people/c1"),
                    "photos": [{"url": "https://lh3.example/photo"}],
                }
            }
        )
        encoded = base64.b64encode(PNG_BYTES).decode()
        out = await _unwrap(manage_contact_photo)(
            service=service,
            user_google_email=EMAIL,
            action="update",
            contact_id="c1",
            image_base64=f"data:image/png;base64,{encoded}",
        )
        kwargs = service.people().updateContactPhoto.call_args.kwargs
        assert kwargs["resourceName"] == "people/c1"
        assert kwargs["body"]["personFields"] == DETAILED_PERSON_FIELDS
        assert base64.b64decode(kwargs["body"]["photoBytes"]) == PNG_BYTES
        assert f"Photo updated ({len(PNG_BYTES)} bytes)" in out
        assert "Photo URL: https://lh3.example/photo" in out

    @pytest.mark.asyncio
    async def test_update_from_url_downloads_server_side(self):
        service = _photo_service()
        seen = []
        with patch.object(
            contacts_helpers,
            "ssrf_safe_stream",
            _fake_stream(_FakeResponse(), seen),
        ):
            out = await _unwrap(manage_contact_photo)(
                service=service,
                user_google_email=EMAIL,
                action="update",
                contact_id="people/c1",
                image_url="https://img.example/a.png",
            )
        assert seen == ["https://img.example/a.png"]
        body = service.people().updateContactPhoto.call_args.kwargs["body"]
        assert base64.b64decode(body["photoBytes"]) == PNG_BYTES
        assert "Photo updated" in out

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "response, match",
        [
            (_FakeResponse(status=404), "HTTP 404"),
            (_FakeResponse(headers={"Content-Type": "text/html"}), "not an image"),
            (
                _FakeResponse(
                    headers={
                        "Content-Type": "image/png",
                        "Content-Length": str(MAX_CONTACT_PHOTO_BYTES + 1),
                    }
                ),
                "limit",
            ),
            (
                _FakeResponse(body=b"x" * (MAX_CONTACT_PHOTO_BYTES + 1), chunk=1 << 20),
                "exceeds",
            ),
            (_FakeResponse(body=b""), "empty"),
        ],
    )
    async def test_update_from_url_rejects_bad_downloads(self, response, match):
        service = _photo_service()
        with patch.object(
            contacts_helpers, "ssrf_safe_stream", _fake_stream(response, [])
        ):
            with pytest.raises(UserInputError, match=match):
                await _unwrap(manage_contact_photo)(
                    service=service,
                    user_google_email=EMAIL,
                    action="update",
                    contact_id="c1",
                    image_url="https://img.example/a.png",
                )
        service.people().updateContactPhoto().execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_rejects_non_http_url_before_fetching(self):
        with patch.object(contacts_helpers, "ssrf_safe_stream") as stream:
            with pytest.raises(UserInputError, match="http"):
                await _unwrap(manage_contact_photo)(
                    service=_photo_service(),
                    user_google_email=EMAIL,
                    action="update",
                    contact_id="c1",
                    image_url="file:///etc/passwd",
                )
        stream.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "kwargs, match",
        [
            ({}, "image_base64 or image_url is required"),
            ({"image_base64": "AAAA", "image_url": "https://x/y"}, "not both"),
            ({"image_base64": "@@"}, "not valid base64"),
        ],
    )
    async def test_update_input_validation(self, kwargs, match):
        service = _photo_service()
        with pytest.raises(UserInputError, match=match):
            await _unwrap(manage_contact_photo)(
                service=service,
                user_google_email=EMAIL,
                action="update",
                contact_id="c1",
                **kwargs,
            )
        service.people().updateContactPhoto().execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_calls_api_and_formats_person(self):
        service = _photo_service(delete_response={"person": _person("people/c1")})
        out = await _unwrap(manage_contact_photo)(
            service=service, user_google_email=EMAIL, action="delete", contact_id="c1"
        )
        assert service.people().deleteContactPhoto.call_args.kwargs == {
            "resourceName": "people/c1",
            "personFields": DETAILED_PERSON_FIELDS,
        }
        assert out.startswith("Photo removed from contact people/c1")
        assert "Contact ID: c1" in out
        service.people().updateContactPhoto().execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_without_person_in_response(self):
        out = await _unwrap(manage_contact_photo)(
            service=_photo_service(),
            user_google_email=EMAIL,
            action="delete",
            contact_id="c1",
        )
        assert out == "Photo removed from contact people/c1 for user@example.com."

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "kwargs",
        [{"action": "set", "contact_id": "c1"}, {"action": "delete", "contact_id": ""}],
    )
    async def test_invalid_action_or_id(self, kwargs):
        with pytest.raises(UserInputError):
            await _unwrap(manage_contact_photo)(
                service=_photo_service(), user_google_email=EMAIL, **kwargs
            )
