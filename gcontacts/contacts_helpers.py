"""
Google Contacts Helper Functions

This module provides pure utility functions for parsing, normalizing,
formatting, and merging Google Contacts (People API) data. These helpers
operate purely on plain dicts/strings and are kept separate from the MCP
tool definitions and Pydantic input models in contacts_tools.py.
"""

import base64
import binascii
import datetime
import logging
import re
from typing import Any, Dict, List, Optional

from core.http_utils import redact_url, ssrf_safe_stream
from core.utils import UserInputError

logger = logging.getLogger(__name__)

# Name fields the People API derives from the structured parts. Sending them
# back alongside an edited part would conflict with (or override) the edit.
_DERIVED_NAME_FIELDS = (
    "metadata",
    "displayName",
    "displayNameLastFirst",
    "unstructuredName",
)


def _parse_birthday(s: str) -> Dict[str, Any]:
    """Parse 'YYYY-MM-DD' or 'MM-DD' into a People API birthday object."""
    parts = s.strip().split("-")
    try:
        if len(parts) == 3:
            year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
        elif len(parts) == 2:
            year, month, day = None, int(parts[0]), int(parts[1])
        else:
            raise ValueError
    except ValueError:
        raise ValueError(
            f"Invalid birthday format '{s}'. Use 'YYYY-MM-DD' or 'MM-DD'."
        ) from None

    if not 1 <= month <= 12 or not 1 <= day <= 31:
        raise ValueError(
            f"Invalid birthday '{s}': month must be 1-12 and day must be 1-31."
        )

    # Reject impossible day/month combinations (e.g. Feb 30, Apr 31) and bad
    # years before they reach the People API. For year-less dates use a leap
    # year (2000) so Feb 29 is accepted.
    try:
        datetime.date(year if year is not None else 2000, month, day)
    except ValueError:
        raise ValueError(f"Invalid birthday '{s}': not a real calendar date.") from None

    date = {"month": month, "day": day}
    if year is not None:
        date["year"] = year
    return {"date": date}


def _normalize_phone(value: str) -> str:
    """Return a normalized phone string for deduplication. Strips non-digit chars except leading +."""
    stripped = re.sub(r"[^\d+]", "", value)
    return stripped.lower()


def _normalize_email(value: str) -> str:
    """Return a normalized email string for deduplication."""
    return value.strip().lower()


def _normalize_nickname(value: str) -> str:
    """Return a normalized nickname string for deduplication."""
    return value.strip().lower()


def _normalize_url(value: str) -> str:
    """Return a normalized URL string for deduplication. Lowercases scheme/host, strips trailing slash."""
    stripped = value.strip().lower()
    if stripped.endswith("/"):
        stripped = stripped[:-1]
    return stripped


def _normalize_user_defined_key(value: str) -> str:
    """Return a normalized userDefined key for deduplication."""
    return value.strip().lower()


def _normalize_relation_person(value: str) -> str:
    """Return a normalized relation person name for deduplication."""
    return value.strip().lower()


def _format_phone_line(phone: Dict[str, Any]) -> str:
    """
    Format a single phone entry into a display line.

    Returns e.g. '+79270000000 (mobile)' or '250 (Internal)'.
    """
    value = phone.get("value", "")
    phone_type = phone.get("type", "")
    formatted_type = phone.get("formattedType", "")

    if phone_type == "internal":
        label = "Internal"
    elif formatted_type:
        label = formatted_type
    elif phone_type:
        label = phone_type
    else:
        label = ""

    if label:
        return f"{value} ({label})"
    return value


def _format_email_line(email: Dict[str, Any]) -> str:
    """
    Format a single email entry into a display line.

    Returns e.g. 'user@example.com (work)'.
    """
    value = email.get("value", "")
    email_type = email.get("type", "")
    formatted_type = email.get("formattedType", "")

    label = formatted_type or email_type or ""
    if label:
        return f"{value} ({label})"
    return value


def _format_contact(person: Dict[str, Any], detailed: bool = False) -> str:
    """
    Format a Person resource into a readable string.

    Args:
        person: The Person resource from the People API.
        detailed: Whether to include detailed fields.

    Returns:
        Formatted string representation of the contact.
    """
    resource_name = person.get("resourceName", "Unknown")
    contact_id = resource_name.replace("people/", "") if resource_name else "Unknown"

    lines = [f"Contact ID: {contact_id}"]

    # Names
    names = person.get("names", [])
    if names:
        primary_name = names[0]
        display_name = primary_name.get("displayName", "")
        if display_name:
            lines.append(f"Name: {display_name}")

    # Nicknames — bilingual alternative names
    nicknames = person.get("nicknames", [])
    if nicknames:
        nickname_values = [n.get("value", "") for n in nicknames if n.get("value")]
        if nickname_values:
            lines.append(f"Nicknames: {', '.join(nickname_values)}")

    # Email addresses — each on its own line with type label
    emails = person.get("emailAddresses", [])
    if emails:
        valid_emails = [e for e in emails if e.get("value")]
        if valid_emails:
            if len(valid_emails) == 1:
                lines.append(f"Email: {_format_email_line(valid_emails[0])}")
            else:
                lines.append("Emails:")
                for e in valid_emails:
                    lines.append(f"  - {_format_email_line(e)}")

    # Phone numbers — each on its own line with type label
    phones = person.get("phoneNumbers", [])
    if phones:
        valid_phones = [p for p in phones if p.get("value")]
        if valid_phones:
            if len(valid_phones) == 1:
                lines.append(f"Phone: {_format_phone_line(valid_phones[0])}")
            else:
                lines.append("Phones:")
                for p in valid_phones:
                    lines.append(f"  - {_format_phone_line(p)}")

    # Organizations
    orgs = person.get("organizations", [])
    if orgs:
        org = orgs[0]
        org_parts = []
        if org.get("title"):
            org_parts.append(org["title"])
        if org.get("name"):
            org_parts.append(f"at {org['name']}")
        if org_parts:
            lines.append(f"Organization: {' '.join(org_parts)}")

    if detailed:
        # Addresses
        addresses = person.get("addresses", [])
        if addresses:
            addr = addresses[0]
            formatted_addr = addr.get("formattedValue", "")
            if formatted_addr:
                lines.append(f"Address: {formatted_addr}")

        # Birthday
        birthdays = person.get("birthdays", [])
        if birthdays:
            bday = birthdays[0].get("date", {})
            if bday:
                bday_str = f"{bday.get('month', '?')}/{bday.get('day', '?')}"
                if bday.get("year"):
                    bday_str = f"{bday.get('year')}/{bday_str}"
                lines.append(f"Birthday: {bday_str}")

        # URLs
        urls = person.get("urls", [])
        if urls:
            url_list = [u.get("value", "") for u in urls if u.get("value")]
            if url_list:
                lines.append(f"URLs: {', '.join(url_list)}")

        # User-defined custom fields
        user_defined = person.get("userDefined", [])
        if user_defined:
            valid_ud = [ud for ud in user_defined if ud.get("key") and ud.get("value")]
            if valid_ud:
                lines.append("Custom Fields:")
                for ud in valid_ud:
                    lines.append(f"  - {ud['key']}: {ud['value']}")

        # Relations (spouse, family, etc.)
        relations = person.get("relations", [])
        if relations:
            valid_rels = [r for r in relations if r.get("person")]
            if valid_rels:
                lines.append("Relations:")
                for r in valid_rels:
                    rel_type = r.get("formattedType") or r.get("type") or ""
                    if rel_type:
                        lines.append(f"  - {r['person']} ({rel_type})")
                    else:
                        lines.append(f"  - {r['person']}")

        # Biography/Notes
        bios = person.get("biographies", [])
        if bios:
            bio = bios[0].get("value", "")
            if bio:
                # Truncate long bios
                if len(bio) > 200:
                    bio = bio[:200] + "..."
                lines.append(f"Notes: {bio}")

        # Metadata
        metadata = person.get("metadata", {})
        if metadata:
            sources = metadata.get("sources", [])
            if sources:
                source_types = [s.get("type", "") for s in sources]
                if source_types:
                    lines.append(f"Sources: {', '.join(source_types)}")

    return "\n".join(lines)


def _merge_phones(
    existing: List[Dict[str, Any]],
    new_phones: List[Dict[str, Any]],
    mode: str,
) -> List[Dict[str, Any]]:
    """
    Merge phone lists according to mode.

    Args:
        existing: Current phoneNumbers from People API.
        new_phones: New phone entries (API format: {value, type?, ...}).
        mode: 'merge', 'replace', or 'remove'.

    Returns:
        Merged phone list.
    """

    def phone_key(phone: Dict[str, Any]) -> str:
        return _normalize_phone(phone.get("canonicalForm") or phone.get("value", ""))

    if mode == "replace":
        return new_phones
    if mode == "remove":
        remove_normalized = {
            _normalize_phone(p["value"]) for p in new_phones if p.get("value")
        }
        return [
            p
            for p in existing
            if _normalize_phone(p.get("value", "")) not in remove_normalized
        ]
    # merge: add new entries that don't already exist (dedup by canonicalForm or normalized value)
    result = list(existing)
    existing_keys = set()
    for p in existing:
        existing_keys.add(phone_key(p))
    for p in new_phones:
        canonical = phone_key(p)
        if canonical not in existing_keys:
            result.append(p)
            existing_keys.add(canonical)
    return result


def _merge_emails(
    existing: List[Dict[str, Any]],
    new_emails: List[Dict[str, Any]],
    mode: str,
) -> List[Dict[str, Any]]:
    """
    Merge email lists according to mode.

    Args:
        existing: Current emailAddresses from People API.
        new_emails: New email entries (API format: {value, type?, ...}).
        mode: 'merge', 'replace', or 'remove'.

    Returns:
        Merged email list.
    """
    if mode == "replace":
        return new_emails
    if mode == "remove":
        remove_normalized = {
            _normalize_email(e["value"]) for e in new_emails if e.get("value")
        }
        return [
            e
            for e in existing
            if _normalize_email(e.get("value", "")) not in remove_normalized
        ]
    # merge: add new entries not already present (dedup by lowercased address)
    result = list(existing)
    existing_keys = {_normalize_email(e.get("value", "")) for e in existing}
    for e in new_emails:
        normalized = _normalize_email(e.get("value", ""))
        if normalized not in existing_keys:
            result.append(e)
            existing_keys.add(normalized)
    return result


def _merge_organizations(
    existing: List[Dict[str, Any]],
    new_orgs: List[Dict[str, Any]],
    mode: str,
) -> List[Dict[str, Any]]:
    """
    Merge organization lists according to mode.

    Args:
        existing: Current organizations from People API.
        new_orgs: New org entries (API format).
        mode: 'merge', 'replace', or 'remove'.

    Returns:
        Merged org list.
    """

    def organization_key(org: Dict[str, Any]) -> tuple[str, str, str, str, str]:
        job_description = org.get("jobDescription") or org.get("description") or ""
        return (
            ((org.get("name") or "").strip().lower()),
            ((org.get("title") or "").strip().lower()),
            ((org.get("department") or "").strip().lower()),
            job_description.strip().lower(),
            ((org.get("type") or "").strip().lower()),
        )

    if mode == "replace":
        return new_orgs
    if mode == "remove":
        remove_keys = {organization_key(org) for org in new_orgs}
        return [org for org in existing if organization_key(org) not in remove_keys]
    # merge: add orgs whose identifying fields don't already exist
    result = list(existing)
    existing_keys = {organization_key(org) for org in existing}
    for org in new_orgs:
        org_key = organization_key(org)
        if org_key not in existing_keys:
            result.append(org)
            existing_keys.add(org_key)
    return result


def _merge_nicknames(
    existing: List[Dict[str, Any]],
    new_nicknames: List[Dict[str, Any]],
    mode: str,
) -> List[Dict[str, Any]]:
    """
    Merge nickname lists according to mode. Dedup by lowercased value.
    """
    if mode == "replace":
        return new_nicknames
    if mode == "remove":
        remove_normalized = {
            _normalize_nickname(n["value"]) for n in new_nicknames if n.get("value")
        }
        return [
            n
            for n in existing
            if _normalize_nickname(n.get("value", "")) not in remove_normalized
        ]
    # merge: add new entries not already present
    result = list(existing)
    existing_keys = {_normalize_nickname(n.get("value", "")) for n in existing}
    for n in new_nicknames:
        normalized = _normalize_nickname(n.get("value", ""))
        if normalized not in existing_keys:
            result.append(n)
            existing_keys.add(normalized)
    return result


def _merge_urls(
    existing: List[Dict[str, Any]],
    new_urls: List[Dict[str, Any]],
    mode: str,
) -> List[Dict[str, Any]]:
    """
    Merge URL lists according to mode. Dedup by normalized URL (lowercased, trailing slash stripped).
    """
    if mode == "replace":
        return new_urls
    if mode == "remove":
        remove_normalized = {
            _normalize_url(u["value"]) for u in new_urls if u.get("value")
        }
        return [
            u
            for u in existing
            if _normalize_url(u.get("value", "")) not in remove_normalized
        ]
    # merge: add new entries not already present
    result = list(existing)
    existing_keys = {_normalize_url(u.get("value", "")) for u in existing}
    for u in new_urls:
        normalized = _normalize_url(u.get("value", ""))
        if normalized not in existing_keys:
            result.append(u)
            existing_keys.add(normalized)
    return result


def _merge_user_defined(
    existing: List[Dict[str, Any]],
    new_ud: List[Dict[str, Any]],
    mode: str,
) -> List[Dict[str, Any]]:
    """
    Merge userDefined custom-field lists according to mode. Dedup by normalized key.
    On merge, new value overrides existing for the same key.
    """
    if mode == "replace":
        return new_ud
    if mode == "remove":
        remove_keys = {
            _normalize_user_defined_key(ud["key"]) for ud in new_ud if ud.get("key")
        }
        return [
            ud
            for ud in existing
            if _normalize_user_defined_key(ud.get("key", "")) not in remove_keys
        ]
    # merge: new value overrides existing for matching key; new keys appended
    new_by_key = {_normalize_user_defined_key(ud.get("key", "")): ud for ud in new_ud}
    result = []
    seen_keys = set()
    for ud in existing:
        norm_key = _normalize_user_defined_key(ud.get("key", ""))
        if norm_key in new_by_key:
            result.append(new_by_key[norm_key])
            seen_keys.add(norm_key)
        else:
            result.append(ud)
    for norm_key, ud in new_by_key.items():
        if norm_key not in seen_keys:
            result.append(ud)
    return result


def _merge_relations(
    existing: List[Dict[str, Any]],
    new_relations: List[Dict[str, Any]],
    mode: str,
) -> List[Dict[str, Any]]:
    """
    Merge relations lists according to mode. Dedup by (normalized person, normalized type) tuple.
    """

    def relation_key(rel: Dict[str, Any]) -> tuple[str, str]:
        label = rel.get("formattedType") or rel.get("type") or ""
        return (
            _normalize_relation_person(rel.get("person", "")),
            label.strip().lower(),
        )

    if mode == "replace":
        return new_relations
    if mode == "remove":
        remove_keys = {relation_key(r) for r in new_relations}
        return [r for r in existing if relation_key(r) not in remove_keys]
    # merge: add new entries not already present
    result = list(existing)
    existing_keys = {relation_key(r) for r in existing}
    for r in new_relations:
        rk = relation_key(r)
        if rk not in existing_keys:
            result.append(r)
            existing_keys.add(rk)
    return result


def _merge_names(
    existing: List[Dict[str, Any]],
    new_names: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Overlay the provided name parts onto the contact's existing name, so changing
    one part (e.g. givenName) keeps the rest (familyName, middleName, suffix...).
    """
    current = next(
        (
            name
            for name in existing
            if name.get("metadata", {}).get("source", {}).get("type") == "CONTACT"
        ),
        {},
    )
    unstructured = current.get("unstructuredName")
    if unstructured and unstructured != " ".join(
        current[part]
        for part in (
            "honorificPrefix",
            "givenName",
            "middleName",
            "familyName",
            "honorificSuffix",
        )
        if current.get(part)
    ):
        raise UserInputError(
            "Cannot partially update a name with an unstructuredName that "
            "cannot be represented by its structured parts."
        )
    kept = {k: v for k, v in current.items() if k not in _DERIVED_NAME_FIELDS}
    return [{**kept, **new_names[0]}]


# =============================================================================
# "Other contacts", directory and photo helpers
# =============================================================================

# otherContacts.search only supports these four fields; otherContacts.list adds
# "photos" when reading READ_SOURCE_TYPE_CONTACT.
OTHER_CONTACTS_READ_MASK = "names,emailAddresses,phoneNumbers,metadata"

# Field mask used for Workspace directory lookups.
DIRECTORY_READ_MASK = "names,emailAddresses,phoneNumbers,organizations"

_DIRECTORY_SOURCE_PREFIX = "DIRECTORY_SOURCE_TYPE_"
DIRECTORY_SOURCE_TYPES = ("DOMAIN_PROFILE", "DOMAIN_CONTACT")

# Contact photos are small images; refuse anything that is clearly not one.
MAX_CONTACT_PHOTO_BYTES = 10 * 1024 * 1024  # 10 MiB
_PHOTO_DOWNLOAD_CHUNK_BYTES = 256 * 1024  # 256 KiB
_REJECTED_PHOTO_CONTENT_TYPES = ("text/", "application/json", "application/xml")


def _normalize_resource_name(contact_id: str, prefix: str = "people") -> str:
    """
    Expand a bare ID into a People API resource name.

    Args:
        contact_id: "c123", "people/c123" or "otherContacts/c123".
        prefix: Collection to assume when the ID carries none.

    Returns:
        The resource name, e.g. "people/c123".
    """
    contact_id = (contact_id or "").strip()
    if not contact_id:
        raise UserInputError("contact_id must not be empty.")
    if "/" in contact_id:
        return contact_id
    return f"{prefix}/{contact_id}"


def _normalize_read_mask(read_mask: Optional[str], default: str) -> str:
    """Trim a comma-separated field mask, falling back to ``default``."""
    if not read_mask:
        return default
    fields = [f.strip() for f in read_mask.split(",") if f.strip()]
    if not fields:
        return default
    return ",".join(fields)


def _normalize_directory_sources(sources: Optional[List[str]]) -> List[str]:
    """
    Accept short ("DOMAIN_PROFILE") or full ("DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE")
    directory source names and return the full enum values the API expects.
    Defaults to both source types.
    """
    if not sources:
        return [f"{_DIRECTORY_SOURCE_PREFIX}{t}" for t in DIRECTORY_SOURCE_TYPES]
    normalized: List[str] = []
    for source in sources:
        value = (source or "").strip().upper()
        if value.startswith(_DIRECTORY_SOURCE_PREFIX):
            value = value[len(_DIRECTORY_SOURCE_PREFIX) :]
        if value not in DIRECTORY_SOURCE_TYPES:
            raise UserInputError(
                f"Invalid directory source '{source}'. "
                f"Must be one of: {', '.join(DIRECTORY_SOURCE_TYPES)}."
            )
        full = f"{_DIRECTORY_SOURCE_PREFIX}{value}"
        if full not in normalized:
            normalized.append(full)
    return normalized


def _format_person_page(
    people: List[Dict[str, Any]],
    heading: str,
    empty_message: str,
    next_page_token: Optional[str] = None,
    total_size: Optional[int] = None,
) -> str:
    """
    Render a page of Person resources (list or search results).

    Args:
        people: Person resources (a search result's ``person`` already unwrapped).
        heading: First line, without the trailing count.
        empty_message: Returned verbatim when ``people`` is empty.
        next_page_token: Surfaced so the caller can continue paginating.
        total_size: Total count across pages when the API reports one.
    """
    if not people:
        return empty_message
    count = (
        f"{len(people)} of {total_size}" if total_size is not None else str(len(people))
    )
    lines = [f"{heading} ({count}):", ""]
    for person in people:
        lines.append(_format_contact(person))
        lines.append("")
    if next_page_token:
        lines.append(f"Next page token: {next_page_token}")
    return "\n".join(lines).rstrip("\n") + ("\n" if not next_page_token else "")


def _unwrap_search_results(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Pull the ``person`` out of each People API SearchResult."""
    return [item.get("person", {}) for item in results if item.get("person")]


def _decode_photo_base64(image_base64: str) -> bytes:
    """
    Decode a base64 image, tolerating a ``data:image/...;base64,`` prefix and
    whitespace. Raises UserInputError on malformed or empty input.
    """
    data = (image_base64 or "").strip()
    if data.startswith("data:"):
        if "," not in data:
            raise UserInputError("image_base64 data URL is missing its ',' separator.")
        header, data = data.split(",", 1)
        if ";base64" not in header:
            raise UserInputError("image_base64 data URL must be base64-encoded.")
    data = "".join(data.split())
    if not data:
        raise UserInputError("image_base64 must not be empty.")
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise UserInputError(f"image_base64 is not valid base64: {exc}") from exc
    if not raw:
        raise UserInputError("image_base64 decoded to zero bytes.")
    if len(raw) > MAX_CONTACT_PHOTO_BYTES:
        raise UserInputError(
            f"Photo is {len(raw)} bytes; the limit is {MAX_CONTACT_PHOTO_BYTES} bytes."
        )
    return raw


async def _download_photo_from_url(image_url: str) -> bytes:
    """
    Download an image server-side with SSRF protection and a size cap.

    Args:
        image_url: http(s) URL of the image.

    Returns:
        Raw image bytes.
    """
    url = (image_url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        raise UserInputError("image_url must start with http:// or https://.")
    redacted = redact_url(url)

    chunks: List[bytes] = []
    total = 0
    async with ssrf_safe_stream(url) as resp:
        if resp.status_code != 200:
            raise UserInputError(
                f"Failed to download image from {redacted} (HTTP {resp.status_code})."
            )
        content_type = (resp.headers.get("Content-Type") or "").lower()
        if content_type.startswith(_REJECTED_PHOTO_CONTENT_TYPES):
            raise UserInputError(
                f"URL {redacted} returned '{content_type}', not an image."
            )
        content_length = resp.headers.get("Content-Length")
        if content_length and content_length.isdigit():
            if int(content_length) > MAX_CONTACT_PHOTO_BYTES:
                raise UserInputError(
                    f"Image at {redacted} is {content_length} bytes; the limit is "
                    f"{MAX_CONTACT_PHOTO_BYTES} bytes."
                )
        async for chunk in resp.aiter_bytes(chunk_size=_PHOTO_DOWNLOAD_CHUNK_BYTES):
            total += len(chunk)
            if total > MAX_CONTACT_PHOTO_BYTES:
                raise UserInputError(
                    f"Image at {redacted} exceeds the {MAX_CONTACT_PHOTO_BYTES} byte limit."
                )
            chunks.append(chunk)

    raw = b"".join(chunks)
    if not raw:
        raise UserInputError(f"Image at {redacted} was empty.")
    return raw


async def _resolve_photo_bytes(
    image_base64: Optional[str], image_url: Optional[str]
) -> bytes:
    """
    Turn exactly one of ``image_base64`` / ``image_url`` into raw photo bytes.
    """
    has_b64 = bool(image_base64 and image_base64.strip())
    has_url = bool(image_url and image_url.strip())
    if has_b64 and has_url:
        raise UserInputError("Provide either image_base64 or image_url, not both.")
    if has_b64:
        return _decode_photo_base64(image_base64)
    if has_url:
        return await _download_photo_from_url(image_url)
    raise UserInputError("image_base64 or image_url is required for 'update' action.")


def _encode_photo_bytes(raw: bytes) -> str:
    """Base64-encode raw bytes for the People API ``photoBytes`` field."""
    return base64.b64encode(raw).decode("ascii")
