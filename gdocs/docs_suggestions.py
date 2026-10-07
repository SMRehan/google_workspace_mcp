"""
Tracked-suggestion helpers for Google Docs.

A document fetched with suggestionsViewMode=SUGGESTIONS_INLINE carries every
pending suggestion as IDs scattered over text runs, paragraphs, tables and
objects. collect_suggestions walks that structure once and returns one record
per suggestion ID so a caller can review and then accept, reject or delete by
ID through documents.batchUpdate.
"""

import json
import logging
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

SUGGESTION_ACTIONS = ("list", "accept", "reject", "delete")

# Field name on a Docs element -> suggestion kind reported to the caller.
_LIST_FIELDS = {
    "suggestedInsertionIds": "insertion",
    "suggestedDeletionIds": "deletion",
}
_SCALAR_FIELDS = {
    "suggestedInsertionId": "insertion",
}
_MAP_FIELDS = {
    "suggestedTextStyleChanges": "text_style",
    "suggestedParagraphStyleChanges": "paragraph_style",
    "suggestedBulletChanges": "bullet",
    "suggestedPositionedObjectIds": "positioned_object",
    "suggestedTableRowStyleChanges": "table_row_style",
    "suggestedTableCellStyleChanges": "table_cell_style",
    "suggestedInlineObjectPropertiesChanges": "inline_object_properties",
    "suggestedPositionedObjectPropertiesChanges": "positioned_object_properties",
    "suggestedDocumentStyleChanges": "document_style",
    "suggestedNamedStylesChanges": "named_styles",
    "suggestedDropdownPropertiesChanges": "dropdown_properties",
    "suggestedSectionStyleChanges": "section_style",
    "suggestedTableStyleChanges": "table_style",
}

SNIPPET_LIMIT = 80


class SuggestionCollector:
    """Accumulates suggestion records while walking a document."""

    def __init__(self) -> None:
        self.records: Dict[str, Dict[str, Any]] = {}
        self._order: List[str] = []
        # One run can carry the same ID in several fields (an inserted run whose
        # style is also suggested); its text must only enter the snippet once.
        self._snippet_spans: set = set()

    def add(
        self,
        suggestion_id: str,
        kind: str,
        tab_id: Optional[str],
        segment_id: Optional[str],
        start_index: Optional[int] = None,
        end_index: Optional[int] = None,
        snippet: str = "",
    ) -> None:
        if not suggestion_id:
            return
        record = self.records.get(suggestion_id)
        if record is None:
            record = {
                "suggestion_id": suggestion_id,
                "types": [],
                "tab_id": tab_id,
                "segment_id": segment_id,
                "start_index": start_index,
                "end_index": end_index,
                "snippet": "",
            }
            self.records[suggestion_id] = record
            self._order.append(suggestion_id)
        if kind not in record["types"]:
            record["types"].append(kind)
        if start_index is not None:
            if record["start_index"] is None or start_index < record["start_index"]:
                record["start_index"] = start_index
        if end_index is not None:
            if record["end_index"] is None or end_index > record["end_index"]:
                record["end_index"] = end_index
        span = (suggestion_id, start_index, end_index)
        if snippet and span not in self._snippet_spans:
            self._snippet_spans.add(span)
            if len(record["snippet"]) < SNIPPET_LIMIT:
                record["snippet"] = (record["snippet"] + snippet)[:SNIPPET_LIMIT]

    def results(self) -> List[Dict[str, Any]]:
        def sort_key(suggestion_id: str):
            record = self.records[suggestion_id]
            start = record["start_index"]
            return (start is None, start if start is not None else 0)

        ordered = sorted(self._order, key=sort_key)
        return [self.records[suggestion_id] for suggestion_id in ordered]


def _scan_node(
    node: Dict[str, Any],
    collector: SuggestionCollector,
    tab_id: Optional[str],
    segment_id: Optional[str],
    start_index: Optional[int],
    end_index: Optional[int],
    snippet: str = "",
) -> None:
    """Record every suggestion field present directly on one element."""
    for field, kind in _LIST_FIELDS.items():
        for suggestion_id in node.get(field) or []:
            collector.add(
                suggestion_id, kind, tab_id, segment_id, start_index, end_index, snippet
            )
    for field, kind in _SCALAR_FIELDS.items():
        suggestion_id = node.get(field)
        if suggestion_id:
            collector.add(
                suggestion_id, kind, tab_id, segment_id, start_index, end_index, snippet
            )
    for field, kind in _MAP_FIELDS.items():
        for suggestion_id in (node.get(field) or {}).keys():
            collector.add(
                suggestion_id, kind, tab_id, segment_id, start_index, end_index, snippet
            )


def _element_snippet(element: Dict[str, Any]) -> str:
    text_run = element.get("textRun")
    if text_run and "content" in text_run:
        return text_run["content"].replace("\n", " ")
    for key in ("person", "richLink", "dateElement", "dropdown"):
        if key in element:
            props = element[key]
            for prop_key in (
                "personProperties",
                "richLinkProperties",
                "dateElementProperties",
                "dropdownProperties",
            ):
                props_value = props.get(prop_key)
                if isinstance(props_value, dict):
                    value = (
                        props_value.get("displayText")
                        or props_value.get("displayValue")
                        or props_value.get("title")
                        or props_value.get("name")
                        or props_value.get("email")
                    )
                    if value:
                        return f"[{key}: {value}]"
            return f"[{key}]"
    for key in (
        "inlineObjectElement",
        "footnoteReference",
        "pageBreak",
        "horizontalRule",
        "equation",
        "columnBreak",
        "autoText",
    ):
        if key in element:
            return f"[{key}]"
    return ""


def _scan_structural_elements(
    elements: Iterable[Dict[str, Any]],
    collector: SuggestionCollector,
    tab_id: Optional[str],
    segment_id: Optional[str],
    depth: int = 0,
) -> None:
    if depth > 8:
        return
    for element in elements:
        start = element.get("startIndex")
        end = element.get("endIndex")
        paragraph = element.get("paragraph")
        if paragraph is not None:
            _scan_node(paragraph, collector, tab_id, segment_id, start, end)
            for pe in paragraph.get("elements", []):
                pe_start = pe.get("startIndex", start)
                pe_end = pe.get("endIndex", end)
                snippet = _element_snippet(pe)
                # A text run's suggestion IDs live on the run itself; every
                # other element type nests them one level down.
                carriers = [pe.get("textRun")] if pe.get("textRun") else []
                carriers.extend(
                    pe[key]
                    for key in pe
                    if key not in ("startIndex", "endIndex", "textRun")
                    and isinstance(pe[key], dict)
                )
                for carrier in carriers:
                    _scan_node(
                        carrier,
                        collector,
                        tab_id,
                        segment_id,
                        pe_start,
                        pe_end,
                        snippet,
                    )
            continue

        table = element.get("table")
        if table is not None:
            _scan_node(table, collector, tab_id, segment_id, start, end)
            for row in table.get("tableRows", []):
                _scan_node(
                    row,
                    collector,
                    tab_id,
                    segment_id,
                    row.get("startIndex", start),
                    row.get("endIndex", end),
                )
                for cell in row.get("tableCells", []):
                    _scan_node(
                        cell,
                        collector,
                        tab_id,
                        segment_id,
                        cell.get("startIndex", start),
                        cell.get("endIndex", end),
                    )
                    _scan_structural_elements(
                        cell.get("content", []),
                        collector,
                        tab_id,
                        segment_id,
                        depth + 1,
                    )
            continue

        section_break = element.get("sectionBreak")
        if section_break is not None:
            _scan_node(section_break, collector, tab_id, segment_id, start, end)
            continue

        toc = element.get("tableOfContents")
        if toc is not None:
            _scan_node(toc, collector, tab_id, segment_id, start, end)
            _scan_structural_elements(
                toc.get("content", []), collector, tab_id, segment_id, depth + 1
            )


def _scan_document_tab(
    document_tab: Dict[str, Any], collector: SuggestionCollector, tab_id: Optional[str]
) -> None:
    _scan_node(document_tab, collector, tab_id, None, None, None)
    _scan_structural_elements(
        document_tab.get("body", {}).get("content", []), collector, tab_id, None
    )
    for kind in ("headers", "footers", "footnotes"):
        for segment_id, segment in (document_tab.get(kind) or {}).items():
            _scan_structural_elements(
                segment.get("content", []), collector, tab_id, segment_id
            )
    for kind in ("inlineObjects", "positionedObjects"):
        for object_id, obj in (document_tab.get(kind) or {}).items():
            _scan_node(
                obj, collector, tab_id, None, None, None, f"[{kind}:{object_id}]"
            )


def _walk_tabs(tabs: List[Dict[str, Any]], collector: SuggestionCollector) -> None:
    for tab in tabs:
        tab_id = tab.get("tabProperties", {}).get("tabId")
        document_tab = tab.get("documentTab")
        if document_tab:
            _scan_document_tab(document_tab, collector, tab_id)
        _walk_tabs(tab.get("childTabs", []), collector)


def collect_suggestions(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    List every pending suggestion in a document fetched with SUGGESTIONS_INLINE.

    Returns one dict per suggestion ID with keys suggestion_id, types (e.g.
    ["insertion"], ["deletion", "text_style"]), tab_id, segment_id,
    start_index, end_index and a short text snippet, ordered by position.
    """
    collector = SuggestionCollector()
    tabs = doc.get("tabs") or []
    if tabs:
        _walk_tabs(tabs, collector)
    else:
        # Legacy top-level fields for documents fetched without includeTabsContent.
        _scan_document_tab(doc, collector, None)
    return collector.results()


def format_suggestions(
    suggestions: List[Dict[str, Any]], document_id: str, tab_id: Optional[str] = None
) -> str:
    """Render collected suggestions as numbered, ID-bearing lines."""
    if tab_id:
        suggestions = [s for s in suggestions if s.get("tab_id") == tab_id]
    if not suggestions:
        scope = f" in tab {tab_id}" if tab_id else ""
        return f"No pending suggestions found in document {document_id}{scope}."

    lines = [f"Found {len(suggestions)} suggestion(s) in document {document_id}:"]
    for number, suggestion in enumerate(suggestions, 1):
        location = ""
        if suggestion.get("start_index") is not None:
            location = f" [{suggestion['start_index']}-{suggestion['end_index']}]"
        where = []
        if suggestion.get("tab_id"):
            where.append(f"tab={suggestion['tab_id']}")
        if suggestion.get("segment_id"):
            where.append(f"segment={suggestion['segment_id']}")
        where_text = f" ({', '.join(where)})" if where else ""
        snippet = suggestion.get("snippet", "").strip()
        snippet_text = f': "{snippet}"' if snippet else ""
        lines.append(
            f"{number}. suggestion_id={suggestion['suggestion_id']} "
            f"type={'+'.join(suggestion['types'])}{location}{where_text}{snippet_text}"
        )
    lines.append(
        "Use manage_doc_suggestions with action=accept, reject or delete and these suggestion_ids."
    )
    return "\n".join(lines)


def normalize_suggestion_ids(suggestion_ids: Any) -> List[str]:
    """Accept a list of IDs, a single ID, or a JSON-encoded list and return a clean list."""
    if suggestion_ids is None:
        return []
    if isinstance(suggestion_ids, str):
        stripped = suggestion_ids.strip()
        if stripped.startswith("["):
            try:
                suggestion_ids = json.loads(stripped)
            except json.JSONDecodeError:
                suggestion_ids = [stripped]
        else:
            suggestion_ids = [stripped] if stripped else []
    if not isinstance(suggestion_ids, list):
        raise ValueError("suggestion_ids must be a list of suggestion ID strings.")
    cleaned = []
    for suggestion_id in suggestion_ids:
        if not isinstance(suggestion_id, str) or not suggestion_id.strip():
            raise ValueError("suggestion_ids must contain non-empty strings.")
        if suggestion_id not in cleaned:
            cleaned.append(suggestion_id.strip())
    return cleaned


def create_suggestion_action_request(action: str, suggestion_id: str) -> Dict[str, Any]:
    """Build an acceptSuggestion / rejectSuggestion / deleteSuggestion request."""
    request_name = {
        "accept": "acceptSuggestion",
        "reject": "rejectSuggestion",
        "delete": "deleteSuggestion",
    }.get(action)
    if request_name is None:
        raise ValueError(
            f"Unsupported suggestion action '{action}'. Use accept, reject or delete."
        )
    return {request_name: {"suggestionId": suggestion_id}}
