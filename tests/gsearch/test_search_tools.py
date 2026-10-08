"""
Unit tests for Google Custom Search tools.

Covers the site_restrict switch (cse.siterestrict vs cse.list) and the
tolerance of get_search_engine_info / search_custom to sparse API responses.
"""

import os
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from gsearch.search_tools import get_search_engine_info, search_custom

EMAIL = "user@example.com"


def _unwrap(tool):
    """Unwrap FunctionTool + decorators to the original async function."""
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


@pytest.fixture(autouse=True)
def _pse_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_PSE_API_KEY", "test-key")
    monkeypatch.setenv("GOOGLE_PSE_ENGINE_ID", "cx-123")


def _service(standard=None, restricted=None):
    service = Mock()
    service.cse().list().execute = Mock(return_value=standard)
    service.cse().siterestrict().list().execute = Mock(return_value=restricted)
    return service


FULL_RESULT = {
    "searchInformation": {"totalResults": "2", "searchTime": 0.25},
    "items": [
        {"title": "One", "link": "https://a.example/1", "snippet": "first\nline"},
        {"title": "Two", "link": "https://a.example/2", "snippet": "second"},
    ],
    "queries": {"nextPage": [{"startIndex": 11}]},
}


class TestSearchCustomSiteRestrict:
    @pytest.mark.asyncio
    async def test_default_uses_standard_endpoint(self):
        service = _service(standard=FULL_RESULT)
        out = await _unwrap(search_custom)(
            service=service, user_google_email=EMAIL, q="hello"
        )
        service.cse().list.assert_called()
        assert service.cse().list.call_args.kwargs["q"] == "hello"
        assert service.cse().list.call_args.kwargs["cx"] == "cx-123"
        service.cse().siterestrict().list().execute.assert_not_called()
        assert "1. One" in out and "Snippet: first line" in out
        assert "Endpoint: siterestrict" not in out
        assert "search again with start=11" in out

    @pytest.mark.asyncio
    async def test_site_restrict_uses_siterestrict_endpoint(self):
        service = _service(restricted=FULL_RESULT)
        out = await _unwrap(search_custom)(
            service=service,
            user_google_email=EMAIL,
            q="hello",
            site_restrict=True,
            num=5,
        )
        kwargs = service.cse().siterestrict().list.call_args.kwargs
        assert kwargs["q"] == "hello"
        assert kwargs["num"] == 5
        assert kwargs["key"] == "test-key"
        service.cse().list().execute.assert_not_called()
        assert "Endpoint: siterestrict" in out
        assert "2. Two" in out

    @pytest.mark.asyncio
    @pytest.mark.parametrize("site_restrict", [False, True])
    async def test_tolerates_sparse_response(self, site_restrict):
        sparse = {"queries": {}}
        service = _service(standard=sparse, restricted=sparse)
        out = await _unwrap(search_custom)(
            service=service, user_google_email=EMAIL, q="x", site_restrict=site_restrict
        )
        assert "Total Results: 0" in out
        assert "Search Time: 0.000 seconds" in out
        assert "No results found." in out
        assert "search again" not in out

    @pytest.mark.asyncio
    async def test_tolerates_none_response(self):
        service = _service(standard=None)
        out = await _unwrap(search_custom)(
            service=service, user_google_email=EMAIL, q="x"
        )
        assert "No results found." in out


class TestGetSearchEngineInfo:
    @pytest.mark.asyncio
    async def test_full_response(self):
        service = _service(
            standard={
                "context": {
                    "title": "My Engine",
                    "facets": [[{"label": "Docs", "anchor": "Documentation"}]],
                },
                "searchInformation": {"totalResults": "120"},
            }
        )
        out = await _unwrap(get_search_engine_info)(
            service=service, user_google_email=EMAIL
        )
        assert "Title: My Engine" in out
        assert "- Docs (anchor: Documentation)" in out
        assert "Total indexed results: 120" in out

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "response",
        [
            {},
            None,
            {"context": None, "searchInformation": None},
            {"context": {"title": None, "facets": None}},
            {"context": {"facets": ["oops", [None, {"label": "L"}]]}},
        ],
    )
    async def test_tolerates_missing_results(self, response):
        service = _service(standard=response)
        out = await _unwrap(get_search_engine_info)(
            service=service, user_google_email=EMAIL
        )
        assert "Search Engine ID: cx-123" in out
        assert "Title: Unknown" in out
        assert "Search Statistics: unavailable" in out

    @pytest.mark.asyncio
    async def test_malformed_facet_entries_are_skipped(self):
        service = _service(
            standard={"context": {"facets": ["oops", [None, {"label": "L"}]]}}
        )
        out = await _unwrap(get_search_engine_info)(
            service=service, user_google_email=EMAIL
        )
        assert "- L (anchor: Unknown)" in out
