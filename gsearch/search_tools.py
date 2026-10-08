"""
Google Custom Search (PSE) MCP Tools

This module provides MCP tools for interacting with Google Programmable Search Engine.
"""

import logging
import asyncio
import os
from typing import Optional, Literal

from mcp.types import ToolAnnotations

from auth.service_decorator import require_google_service
from core.server import server
from core.utils import handle_http_errors, StringList

logger = logging.getLogger(__name__)


def _as_float(value, default: float = 0.0) -> float:
    """Coerce an API-reported number (which may be absent or a string) to float."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@server.tool(
    title="Search Custom",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@handle_http_errors("search_custom", is_read_only=True, service_type="customsearch")
@require_google_service("customsearch", "customsearch")
async def search_custom(
    service,
    user_google_email: str,
    q: str,
    num: int = 10,
    start: int = 1,
    safe: Literal["active", "moderate", "off"] = "off",
    search_type: Optional[Literal["image"]] = None,
    site_search: Optional[str] = None,
    site_search_filter: Optional[Literal["e", "i"]] = None,
    date_restrict: Optional[str] = None,
    file_type: Optional[str] = None,
    language: Optional[str] = None,
    country: Optional[str] = None,
    sites: Optional[StringList] = None,
    site_restrict: bool = False,
) -> str:
    """
    Performs a search using Google Custom Search JSON API.

    Args:
        user_google_email (str): The user's Google email address. Required.
        q (str): The search query. Required.
        num (int): Number of results to return (1-10). Defaults to 10.
        start (int): The index of the first result to return (1-based). Defaults to 1.
        safe (Literal["active", "moderate", "off"]): Safe search level. Defaults to "off".
        search_type (Optional[Literal["image"]]): Search for images if set to "image".
        site_search (Optional[str]): Restrict search to a specific site/domain.
        site_search_filter (Optional[Literal["e", "i"]]): Exclude ("e") or include ("i") site_search results.
        date_restrict (Optional[str]): Restrict results by date (e.g., "d5" for past 5 days, "m3" for past 3 months).
        file_type (Optional[str]): Filter by file type (e.g., "pdf", "doc").
        language (Optional[str]): Language code for results (e.g., "lang_en").
        country (Optional[str]): Country code for results (e.g., "countryUS").
        sites (Optional[List[str]]): List of sites/domains to restrict search to (e.g., ["example.com", "docs.example.com"]). When provided, results are limited to these sites.
        site_restrict (bool): Use the Site Restricted JSON API (cse.siterestrict) instead of the
            standard endpoint. Only valid for engines that search 10 or fewer sites; it has no
            daily query cap. Defaults to False.

    Returns:
        str: Formatted search results including title, link, and snippet for each result.
    """
    # Get API key and search engine ID from environment
    api_key = os.environ.get("GOOGLE_PSE_API_KEY")
    if not api_key:
        raise ValueError(
            "GOOGLE_PSE_API_KEY environment variable not set. Please set it to your Google Custom Search API key."
        )

    cx = os.environ.get("GOOGLE_PSE_ENGINE_ID")
    if not cx:
        raise ValueError(
            "GOOGLE_PSE_ENGINE_ID environment variable not set. Please set it to your Programmable Search Engine ID."
        )

    logger.info(
        f"[search_custom] Invoked. Email: '{user_google_email}', query_len={len(q)}, "
        f"CX: '{cx}', site_restrict={site_restrict}"
    )
    logger.debug(f"[search_custom] Query: '{q}'")

    # Apply site restriction if sites are provided
    if sites:
        site_query = " OR ".join([f"site:{site}" for site in sites])
        q = f"{q} ({site_query})"
        logger.debug(f"[search_custom] Applied site restriction: {sites}")

    # Build the request parameters
    params = {
        "key": api_key,
        "cx": cx,
        "q": q,
        "num": num,
        "start": start,
        "safe": safe,
    }

    # Add optional parameters
    if search_type:
        params["searchType"] = search_type
    if site_search:
        params["siteSearch"] = site_search
    if site_search_filter:
        params["siteSearchFilter"] = site_search_filter
    if date_restrict:
        params["dateRestrict"] = date_restrict
    if file_type:
        params["fileType"] = file_type
    if language:
        params["lr"] = language
    if country:
        params["cr"] = country

    # Execute the search request. The Site Restricted endpoint takes the same
    # parameters but is only served for engines limited to 10 or fewer sites.
    collection = service.cse().siterestrict() if site_restrict else service.cse()
    result = await asyncio.to_thread(collection.list(**params).execute) or {}

    # Extract search information
    search_info = result.get("searchInformation") or {}
    total_results = search_info.get("totalResults", "0")
    search_time = _as_float(search_info.get("searchTime"))

    # Extract search results
    items = result.get("items") or []

    # Format the response
    endpoint_line = "\n- Endpoint: siterestrict" if site_restrict else ""
    confirmation_message = f"""Search Results for {user_google_email}:
- Query: "{q}"
- Search Engine ID: {cx}{endpoint_line}
- Total Results: {total_results}
- Search Time: {search_time:.3f} seconds
- Results Returned: {len(items)} (showing {start} to {start + len(items) - 1})

"""

    if items:
        confirmation_message += "Results:\n"
        for i, item in enumerate(items, start):
            title = item.get("title", "No title")
            link = item.get("link", "No link")
            snippet = item.get("snippet", "No description available").replace("\n", " ")

            confirmation_message += f"\n{i}. {title}\n"
            confirmation_message += f"   URL: {link}\n"
            confirmation_message += f"   Snippet: {snippet}\n"

            # Add additional metadata if available
            if "pagemap" in item:
                pagemap = item["pagemap"]
                if "metatags" in pagemap and pagemap["metatags"]:
                    metatag = pagemap["metatags"][0]
                    if "og:type" in metatag:
                        confirmation_message += f"   Type: {metatag['og:type']}\n"
                    if "article:published_time" in metatag:
                        confirmation_message += (
                            f"   Published: {metatag['article:published_time'][:10]}\n"
                        )
    else:
        confirmation_message += "\nNo results found."

    # Add information about pagination
    queries = result.get("queries") or {}
    if queries.get("nextPage"):
        next_start = queries["nextPage"][0].get("startIndex", 0)
        confirmation_message += (
            f"\n\nTo see more results, search again with start={next_start}"
        )

    logger.info(f"Search completed successfully for {user_google_email}")
    return confirmation_message


@server.tool(
    title="Get Search Engine Info",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@handle_http_errors(
    "get_search_engine_info", is_read_only=True, service_type="customsearch"
)
@require_google_service("customsearch", "customsearch")
async def get_search_engine_info(service, user_google_email: str) -> str:
    """
    Retrieves metadata about a Programmable Search Engine.

    Args:
        user_google_email (str): The user's Google email address. Required.

    Returns:
        str: Information about the search engine including its configuration and available refinements.
    """
    # Get API key and search engine ID from environment
    api_key = os.environ.get("GOOGLE_PSE_API_KEY")
    if not api_key:
        raise ValueError(
            "GOOGLE_PSE_API_KEY environment variable not set. Please set it to your Google Custom Search API key."
        )

    cx = os.environ.get("GOOGLE_PSE_ENGINE_ID")
    if not cx:
        raise ValueError(
            "GOOGLE_PSE_ENGINE_ID environment variable not set. Please set it to your Programmable Search Engine ID."
        )

    logger.info(
        f"[get_search_engine_info] Invoked. Email: '{user_google_email}', CX: '{cx}'"
    )

    # Perform a minimal search to get the search engine context
    params = {
        "key": api_key,
        "cx": cx,
        "q": "test",  # Minimal query to get metadata
        "num": 1,
    }

    result = await asyncio.to_thread(service.cse().list(**params).execute) or {}

    # Extract context information. Engines with no indexed results (or a probe
    # query that matches nothing) may return no "context" or "searchInformation".
    context = result.get("context") or {}
    title = context.get("title") or "Unknown"

    confirmation_message = f"""Search Engine Information for {user_google_email}:
- Search Engine ID: {cx}
- Title: {title}
"""

    # Add facet information if available
    facets = context.get("facets") or []
    refinements = [
        item
        for facet in facets
        if isinstance(facet, list)
        for item in facet
        if isinstance(item, dict)
    ]
    if refinements:
        confirmation_message += "\nAvailable Refinements:\n"
        for item in refinements:
            label = item.get("label", "Unknown")
            anchor = item.get("anchor", "Unknown")
            confirmation_message += f"  - {label} (anchor: {anchor})\n"

    # Add search information
    search_info = result.get("searchInformation") or {}
    if search_info:
        total_results = search_info.get("totalResults", "Unknown")
        confirmation_message += "\nSearch Statistics:\n"
        confirmation_message += f"  - Total indexed results: {total_results}\n"
    else:
        confirmation_message += (
            "\nSearch Statistics: unavailable (the probe query returned no results).\n"
        )

    logger.info(f"Search engine info retrieved successfully for {user_google_email}")
    return confirmation_message
