"""
Google Chat Helper Functions

Request execution, argument normalization, message parsing, and name
resolution for Chat senders and spaces via the People API.
"""

import asyncio
import logging
import re
import ssl
from typing import Dict, List, Optional

from googleapiclient.errors import HttpError

from auth.permissions import is_action_denied
from core.utils import UserInputError

logger = logging.getLogger(__name__)

_SENDER_CACHE_MAX_SIZE = 256
_sender_name_cache: Dict[str, str] = {}
_UNNAMED_SPACE_FALLBACKS = {
    "DIRECT_MESSAGE": "Direct message",
    "GROUP_CHAT": "Group chat",
}
_SPACE_NAME_MAX_MEMBERS = 3
_PEOPLE_BATCH_SIZE = 200  # people.getBatchGet limit
_MAX_CONSECUTIVE_MEMBER_LOOKUP_FAILURES = 2
_SEARCH_MESSAGES_RETRY_BASE_DELAY_SECONDS = 1


def _cache_sender(user_id: str, name: str) -> None:
    """Store a resolved sender name, evicting oldest entries if cache is full."""
    if len(_sender_name_cache) >= _SENDER_CACHE_MAX_SIZE:
        to_remove = list(_sender_name_cache.keys())[: _SENDER_CACHE_MAX_SIZE // 2]
        for k in to_remove:
            del _sender_name_cache[k]
    _sender_name_cache[user_id] = name


def _person_name(person: dict) -> Optional[str]:
    """Return a People API person's display name, falling back to their email."""
    names = person.get("names") or [{}]
    emails = person.get("emailAddresses") or [{}]
    return names[0].get("displayName") or emails[0].get("value")


async def _resolve_sender(people_service, sender_obj: dict) -> str:
    """Resolve a Chat message sender to a display name.

    Fast path: use displayName if the API already provided it.
    Slow path: look up the user via the People API directory and cache the result.
    """
    display_name = sender_obj.get("displayName")
    if display_name:
        return display_name

    user_id = sender_obj.get("name", "")  # e.g. "users/123456789"
    if not user_id:
        return "Unknown Sender"

    if user_id in _sender_name_cache:
        return _sender_name_cache[user_id]

    # Chat API uses "users/ID" but People API expects "people/ID"
    people_resource = user_id.replace("users/", "people/", 1)
    if people_service:
        try:
            person = await asyncio.to_thread(
                people_service.people()
                .get(resourceName=people_resource, personFields="names,emailAddresses")
                .execute
            )
            resolved = _person_name(person)
            if resolved:
                _cache_sender(user_id, resolved)
                return resolved
        except HttpError as e:
            logger.debug(f"People API lookup failed for {user_id}: {e}")
        except Exception as e:
            logger.debug(f"Unexpected error resolving {user_id}: {e}")

    return user_id


async def _lookup_people_names(people_service, user_ids: List[str]) -> Dict[str, str]:
    """Resolve Chat user ids to display names with batched People API calls."""
    names = {}
    missing = []
    for user_id in user_ids:
        if user_id in _sender_name_cache:
            names[user_id] = _sender_name_cache[user_id]
        else:
            missing.append(user_id)
    for start in range(0, len(missing), _PEOPLE_BATCH_SIZE):
        chunk = missing[start : start + _PEOPLE_BATCH_SIZE]
        try:
            response = await asyncio.to_thread(
                people_service.people()
                .getBatchGet(
                    resourceNames=[
                        user_id.replace("users/", "people/", 1) for user_id in chunk
                    ],
                    personFields="names,emailAddresses",
                )
                .execute
            )
        except Exception as e:
            logger.debug(f"People API batch lookup failed: {e}")
            continue
        for entry in response.get("responses", []):
            name = _person_name(entry.get("person") or {})
            requested = entry.get("requestedResourceName")
            if name and requested:
                user_id = requested.replace("people/", "users/", 1)
                names[user_id] = name
                _cache_sender(user_id, name)
    return names


async def _list_memberships(chat_service, space_name: str) -> List[dict]:
    """Return every membership of a space, following nextPageToken."""
    memberships = []
    params = {"parent": space_name, "pageSize": 100}
    while True:
        response = await asyncio.to_thread(
            chat_service.spaces().members().list(**params).execute
        )
        memberships.extend(response.get("memberships", []))
        if not response.get("nextPageToken"):
            return memberships
        params["pageToken"] = response["nextPageToken"]


async def _name_spaces(
    chat_service, people_service, spaces: List[dict]
) -> Dict[str, str]:
    """Map space resource names to labels, naming unnamed spaces after their members."""
    labels = {}
    unnamed = []
    for space in spaces:
        space_name = space.get("name")
        if space_name in labels:
            continue
        if space.get("displayName"):
            labels[space_name] = space["displayName"]
        else:
            labels[space_name] = _UNNAMED_SPACE_FALLBACKS.get(
                space.get("spaceType"), "Unnamed Space"
            )
            unnamed.append(space_name)
    if not unnamed:
        return labels

    # Without the caller's id their own name would end up in every label.
    try:
        me = await asyncio.to_thread(
            people_service.people()
            .get(resourceName="people/me", personFields="metadata")
            .execute
        )
    except Exception as e:
        logger.debug(f"Could not look up the calling user: {e}")
        return labels
    if not me.get("resourceName"):
        return labels
    me_id = me["resourceName"].replace("people/", "users/", 1)

    others_by_space = {}
    consecutive_failures = 0
    for space_name in unnamed:
        try:
            memberships = await _list_memberships(chat_service, space_name)
        except HttpError as e:
            logger.debug(f"Could not list members of {space_name}: {e}")
            # Missing scope or throttling would fail every remaining space too.
            if e.resp.status in (403, 429):
                break
            continue
        except Exception as e:
            logger.debug(f"Could not list members of {space_name}: {e}")
            # Repeated network failures would make every remaining space wait too.
            consecutive_failures += 1
            if consecutive_failures >= _MAX_CONSECUTIVE_MEMBER_LOOKUP_FAILURES:
                break
            continue
        consecutive_failures = 0
        others = []
        for membership in memberships:
            member = membership.get("member", {})
            if member.get("type") == "HUMAN" and member.get("name") != me_id:
                others.append(member.get("name"))
        if others:
            others_by_space[space_name] = others

    shown_ids = []
    for others in others_by_space.values():
        for user_id in others[:_SPACE_NAME_MAX_MEMBERS]:
            if user_id not in shown_ids:
                shown_ids.append(user_id)
    people_names = await _lookup_people_names(people_service, shown_ids)

    for space_name, others in others_by_space.items():
        shown = others[:_SPACE_NAME_MAX_MEMBERS]
        label = ", ".join(people_names.get(user_id, user_id) for user_id in shown)
        remaining = len(others) - len(shown)
        if remaining:
            label += f" and {remaining} other{'s' if remaining > 1 else ''}"
        labels[space_name] = label
    return labels


def _none_if_null_sentinel(value: Optional[str]) -> Optional[str]:
    """Map the literal "null"/"None" some clients send for an omitted arg to None."""
    if value is not None and value.strip().lower() in ("null", "none"):
        return None
    return value


def _none_if_blank(value: Optional[str]) -> Optional[str]:
    """Treat a null sentinel or whitespace-only string as an omitted arg."""
    value = _none_if_null_sentinel(value)
    return value if value and value.strip() else None


async def _execute_chat_request(
    request_factory,
    *,
    request_label: str,
    retries: int = 1,
    semaphore: Optional[asyncio.Semaphore] = None,
):
    """Execute a Chat API request in a worker thread with optional SSL retries."""
    for attempt in range(retries):
        try:
            if semaphore is None:
                return await asyncio.to_thread(lambda: request_factory().execute())
            async with semaphore:
                return await asyncio.to_thread(lambda: request_factory().execute())
        except ssl.SSLError as e:
            if attempt == retries - 1:
                raise
            delay = _SEARCH_MESSAGES_RETRY_BASE_DELAY_SECONDS * (2**attempt)
            logger.warning(
                "[search_messages] SSL error during %s on attempt %s/%s: %s. Retrying in %s seconds.",
                request_label,
                attempt + 1,
                retries,
                e,
                delay,
            )
            await asyncio.sleep(delay)


def _extract_rich_links(msg: dict) -> List[str]:
    """Extract URLs from RICH_LINK annotations (smart chips).

    When a user pastes a Google Workspace URL in Chat and it renders as a
    smart chip, the URL is NOT in the text field; it's only available in
    the annotations array as a RICH_LINK with richLinkMetadata.uri.
    """
    text = msg.get("text", "")
    urls = []
    for ann in msg.get("annotations", []):
        if ann.get("type") == "RICH_LINK":
            uri = ann.get("richLinkMetadata", {}).get("uri", "")
            if uri and uri not in text:
                urls.append(uri)
    return urls


# ---------------------------------------------------------------------------
# Management helpers (members, messages, reactions, direct messages, spaces)
# ---------------------------------------------------------------------------

_MEMBER_ROLES = ("ROLE_MEMBER", "ROLE_MANAGER")
_MEMBER_TYPES = ("HUMAN", "BOT")
_CREATE_SPACE_TYPES = ("SPACE", "GROUP_CHAT")
_SETUP_SPACE_TYPES = ("SPACE", "GROUP_CHAT", "DIRECT_MESSAGE")
_HISTORY_STATES = ("HISTORY_ON", "HISTORY_OFF")
# Field paths a user-authenticated messages.patch may carry. cards/cards_v2/
# accessory_widgets need app authentication and are rejected up front.
_MESSAGE_UPDATE_FIELDS = ("text", "attachment", "quoted_message_metadata")
_SETUP_MAX_MEMBERS = 49
_SEARCH_SPACES_MAX_PAGES = 10

_SPACE_NAME_RE = re.compile(r"spaces/[^/]+")
_MESSAGE_NAME_RE = re.compile(r"spaces/[^/]+/messages/[^/]+")
_MEMBERSHIP_NAME_RE = re.compile(r"spaces/[^/]+/members/[^/]+")
_REACTION_NAME_RE = re.compile(r"spaces/[^/]+/messages/[^/]+/reactions/[^/]+")


def _require_action(action: Optional[str], valid_actions: tuple) -> str:
    """Normalize *action* and reject anything outside *valid_actions*."""
    normalized = (_none_if_blank(action) or "").strip().lower()
    if normalized not in valid_actions:
        raise UserInputError(
            f"Invalid action '{action}'. Must be one of: {', '.join(valid_actions)}"
        )
    return normalized


def _deny_if_restricted(action: str) -> None:
    """Raise when granular permissions deny *action* for the chat service."""
    if is_action_denied("chat", action):
        raise UserInputError(
            f"The '{action}' action is not allowed under the current permission level."
        )


def _require_arg(value: Optional[str], arg_name: str, action: str) -> str:
    """Return a non-blank argument or raise naming the action that needs it."""
    cleaned = _none_if_blank(value)
    if cleaned is None:
        raise UserInputError(f"'{arg_name}' is required for the '{action}' action.")
    return cleaned.strip()


def _require_resource_name(
    value: Optional[str], arg_name: str, action: str, pattern: re.Pattern, example: str
) -> str:
    """Validate a Chat resource name against *pattern*."""
    name = _require_arg(value, arg_name, action)
    if pattern.fullmatch(name) is None:
        raise UserInputError(
            f"Invalid {arg_name} '{name}'. Expected the full resource name, e.g. {example}."
        )
    return name


def _require_choice(
    value: Optional[str], arg_name: str, choices: tuple, default: Optional[str] = None
) -> Optional[str]:
    """Upper-case *value* and check it is one of *choices* (or fall back to default)."""
    cleaned = _none_if_blank(value)
    if cleaned is None:
        return default
    normalized = cleaned.strip().upper()
    if normalized not in choices:
        raise UserInputError(
            f"Invalid {arg_name} '{value}'. Must be one of: {', '.join(choices)}"
        )
    return normalized


def _user_resource_name(user: str) -> str:
    """Map an email, bare id, or users/{x} to the users/{x} form the API expects."""
    user = user.strip()
    if user == "app":
        return "users/app"
    if user.startswith("users/"):
        return user
    return f"users/{user}"


def _quote_filter_value(value: str) -> str:
    """Quote a value for the Chat API filter syntax."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _build_members_filter(
    role: Optional[str], member_type: Optional[str]
) -> Optional[str]:
    """Build the members.list filter from optional role and member type."""
    clauses = []
    if role:
        clauses.append(f"role = {_quote_filter_value(role)}")
    if member_type:
        clauses.append(f"member.type = {_quote_filter_value(member_type)}")
    return " AND ".join(clauses) if clauses else None


def _build_reactions_filter(
    emoji_unicode: Optional[str], user: Optional[str]
) -> Optional[str]:
    """Build the reactions.list filter from optional emoji and user."""
    clauses = []
    if emoji_unicode:
        clauses.append(f"emoji.unicode = {_quote_filter_value(emoji_unicode)}")
    if user:
        clauses.append(f"user.name = {_quote_filter_value(_user_resource_name(user))}")
    return " AND ".join(clauses) if clauses else None


def _resolve_membership_name(
    action: str,
    membership_name: Optional[str],
    space_id: Optional[str],
    member: Optional[str],
) -> str:
    """Return spaces/X/members/Y from an explicit name or a space plus member alias."""
    membership_name = _none_if_blank(membership_name)
    if membership_name is not None:
        return _require_resource_name(
            membership_name,
            "membership_name",
            action,
            _MEMBERSHIP_NAME_RE,
            "spaces/X/members/Y",
        )
    space_id = _none_if_blank(space_id)
    member = _none_if_blank(member)
    if space_id is None or member is None:
        raise UserInputError(
            f"The '{action}' action needs either 'membership_name' "
            "(spaces/X/members/Y) or both 'space_id' and 'member' (email or users/ID)."
        )
    space = _require_resource_name(
        space_id, "space_id", action, _SPACE_NAME_RE, "spaces/X"
    )
    member_id = _user_resource_name(member).removeprefix("users/")
    return f"{space}/members/{member_id}"


def _format_membership(membership: dict) -> str:
    """One-line summary of a Membership resource."""
    member = membership.get("member") or {}
    group = membership.get("groupMember") or {}
    if group:
        who = f"{group.get('name', '?')} (GROUP)"
    else:
        label = member.get("displayName") or member.get("name", "?")
        who = f"{label} ({member.get('type', 'UNKNOWN')})"
        if member.get("displayName") and member.get("name"):
            who = f"{member['displayName']} [{member['name']}] ({member.get('type', 'UNKNOWN')})"
    role = membership.get("role", "MEMBERSHIP_ROLE_UNSPECIFIED")
    state = membership.get("state", "")
    state_suffix = f", state: {state}" if state else ""
    return f"- {who} - role: {role}{state_suffix} (ID: {membership.get('name', '')})"


def _format_space(space: dict) -> str:
    """Multi-line summary of a Space resource."""
    details = space.get("spaceDetails") or {}
    lines = [
        f"Space: {space.get('displayName') or '(unnamed)'} (ID: {space.get('name', '')})",
        f"- Type: {space.get('spaceType', 'UNKNOWN')}",
    ]
    if space.get("spaceHistoryState"):
        lines.append(f"- History: {space['spaceHistoryState']}")
    if "externalUserAllowed" in space:
        lines.append(f"- External users allowed: {space['externalUserAllowed']}")
    if details.get("description"):
        lines.append(f"- Description: {details['description']}")
    if details.get("guidelines"):
        lines.append(f"- Guidelines: {details['guidelines']}")
    if space.get("spaceUri"):
        lines.append(f"- URL: {space['spaceUri']}")
    if space.get("createTime"):
        lines.append(f"- Created: {space['createTime']}")
    return "\n".join(lines)


def _format_message(msg: dict) -> str:
    """Multi-line summary of a Message resource."""
    sender = msg.get("sender") or {}
    sender_label = sender.get("displayName") or sender.get("name", "Unknown Sender")
    lines = [
        f"Message {msg.get('name', '')}",
        f"- From: {sender_label}",
        f"- Created: {msg.get('createTime', 'Unknown Time')}",
    ]
    if msg.get("lastUpdateTime"):
        lines.append(f"- Last updated: {msg['lastUpdateTime']}")
    if msg.get("deleteTime"):
        lines.append(f"- Deleted: {msg['deleteTime']}")
    thread = msg.get("thread") or {}
    if thread.get("name"):
        lines.append(f"- Thread: {thread['name']}")
    lines.append(f"- Text: {msg.get('text') or '(no text)'}")
    for url in _extract_rich_links(msg):
        lines.append(f"- Linked: {url}")
    for idx, att in enumerate(msg.get("attachment", [])):
        lines.append(
            f"- Attachment {idx}: {att.get('contentName', 'unnamed')} "
            f"({att.get('contentType', 'unknown type')})"
        )
    reactions = msg.get("emojiReactionSummaries", [])
    if reactions:
        parts = []
        for r in reactions:
            emoji = r.get("emoji", {})
            symbol = (
                emoji.get("unicode")
                or f":{emoji.get('customEmoji', {}).get('uid', '?')}:"
            )
            parts.append(f"{symbol}x{r.get('reactionCount', 0)}")
        lines.append(f"- Reactions: {', '.join(parts)}")
    return "\n".join(lines)


def _format_reaction(reaction: dict) -> str:
    """One-line summary of a Reaction resource."""
    emoji = reaction.get("emoji") or {}
    symbol = emoji.get("unicode") or f":{emoji.get('customEmoji', {}).get('uid', '?')}:"
    user = reaction.get("user") or {}
    who = user.get("displayName") or user.get("name", "unknown user")
    return f"- {symbol} by {who} (ID: {reaction.get('name', '')})"


def _page_suffix(response: dict, arg_name: str = "page_token") -> str:
    """Trailing line telling the caller how to fetch the next page, if any."""
    token = response.get("nextPageToken")
    return (
        f"\nMore results available. Pass {arg_name}='{token}' to continue."
        if token
        else ""
    )


# --- Members -----------------------------------------------------------------


async def _list_members(
    service,
    space_id: str,
    role: Optional[str],
    member_type: Optional[str],
    page_size: int,
    page_token: Optional[str],
    show_invited: bool,
) -> str:
    """Call spaces.members.list and format one page."""
    params = {"parent": space_id, "pageSize": page_size}
    filter_str = _build_members_filter(role, member_type)
    if filter_str:
        params["filter"] = filter_str
    if page_token:
        params["pageToken"] = page_token
    if show_invited:
        params["showInvited"] = True
    response = await asyncio.to_thread(
        service.spaces().members().list(**params).execute
    )
    memberships = response.get("memberships", [])
    if not memberships:
        criteria = f" matching filter '{filter_str}'" if filter_str else ""
        return f"No members found in space '{space_id}'{criteria}."
    output = [f"Found {len(memberships)} member(s) in space '{space_id}':"]
    output.extend(_format_membership(m) for m in memberships)
    return "\n".join(output) + _page_suffix(response)


async def _get_member(service, membership_name: str) -> str:
    """Call spaces.members.get."""
    membership = await asyncio.to_thread(
        service.spaces().members().get(name=membership_name).execute
    )
    return f"Membership details:\n{_format_membership(membership)}"


async def _add_member(
    service, space_id: str, member: str, role: Optional[str], member_type: str
) -> str:
    """Call spaces.members.create, then patch the role when a manager was asked for."""
    user_name = _user_resource_name(member)
    if user_name == "users/app":
        member_type = "BOT"
    body = {"member": {"name": user_name, "type": member_type}}
    membership = await asyncio.to_thread(
        service.spaces().members().create(parent=space_id, body=body).execute
    )
    created_name = membership.get("name", "")
    # Membership.role is only honoured by UpdateMembership, so a manager role
    # takes a second call against the membership just created.
    if role and role != "ROLE_MEMBER" and created_name:
        membership = await asyncio.to_thread(
            service.spaces()
            .members()
            .patch(name=created_name, updateMask="role", body={"role": role})
            .execute
        )
    state = membership.get("state", "")
    verb = "Invited" if state == "INVITED" else "Added"
    return (
        f"{verb} {user_name} to space '{space_id}':\n{_format_membership(membership)}"
    )


async def _update_member_role(service, membership_name: str, role: str) -> str:
    """Call spaces.members.patch with updateMask=role."""
    membership = await asyncio.to_thread(
        service.spaces()
        .members()
        .patch(name=membership_name, updateMask="role", body={"role": role})
        .execute
    )
    return f"Updated role to {role}:\n{_format_membership(membership)}"


async def _remove_member(service, membership_name: str) -> str:
    """Call spaces.members.delete."""
    await asyncio.to_thread(
        service.spaces().members().delete(name=membership_name).execute
    )
    return f"Removed membership {membership_name}."


# --- Messages ----------------------------------------------------------------


def _parse_message_update_mask(update_mask: Optional[str], text: Optional[str]) -> str:
    """Validate the messages.patch update mask against user-auth field paths."""
    mask = _none_if_blank(update_mask) or "text"
    fields = [f.strip() for f in mask.split(",") if f.strip()]
    invalid = [f for f in fields if f not in _MESSAGE_UPDATE_FIELDS]
    if invalid:
        raise UserInputError(
            f"Unsupported update_mask field(s) {', '.join(invalid)}. "
            f"User authentication supports: {', '.join(_MESSAGE_UPDATE_FIELDS)}."
        )
    if "text" in fields and text is None:
        raise UserInputError("'text' is required when update_mask includes 'text'.")
    return ",".join(fields)


async def _get_message(service, message_name: str) -> str:
    """Call spaces.messages.get."""
    msg = await asyncio.to_thread(
        service.spaces().messages().get(name=message_name).execute
    )
    return _format_message(msg)


async def _update_message(
    service, message_name: str, text: Optional[str], update_mask: Optional[str]
) -> str:
    """Call spaces.messages.patch. Only the author can edit their own message."""
    mask = _parse_message_update_mask(update_mask, text)
    body = {}
    if "text" in mask.split(","):
        body["text"] = text
    message = await asyncio.to_thread(
        service.spaces()
        .messages()
        .patch(name=message_name, updateMask=mask, body=body)
        .execute
    )
    update_time = message.get("lastUpdateTime") or message.get("createTime", "")
    return (
        f"Message updated (fields: {mask}). "
        f"Message ID: {message.get('name', message_name)}, Time: {update_time}"
    )


async def _delete_message(service, message_name: str, force: bool) -> str:
    """Call spaces.messages.delete; force also removes threaded replies."""
    await asyncio.to_thread(
        service.spaces().messages().delete(name=message_name, force=force).execute
    )
    suffix = " along with its threaded replies" if force else ""
    return f"Deleted message {message_name}{suffix}."


# --- Reactions ---------------------------------------------------------------


async def _list_reactions(
    service,
    message_name: str,
    emoji_unicode: Optional[str],
    user: Optional[str],
    page_size: int,
    page_token: Optional[str],
) -> str:
    """Call spaces.messages.reactions.list and format one page."""
    params = {"parent": message_name, "pageSize": page_size}
    filter_str = _build_reactions_filter(emoji_unicode, user)
    if filter_str:
        params["filter"] = filter_str
    if page_token:
        params["pageToken"] = page_token
    response = await asyncio.to_thread(
        service.spaces().messages().reactions().list(**params).execute
    )
    reactions = response.get("reactions", [])
    if not reactions:
        criteria = f" matching filter '{filter_str}'" if filter_str else ""
        return f"No reactions found on message {message_name}{criteria}."
    output = [f"Found {len(reactions)} reaction(s) on message {message_name}:"]
    output.extend(_format_reaction(r) for r in reactions)
    return "\n".join(output) + _page_suffix(response)


async def _delete_reaction(service, reaction_name: str) -> str:
    """Call spaces.messages.reactions.delete. Only the reacting user can delete."""
    await asyncio.to_thread(
        service.spaces().messages().reactions().delete(name=reaction_name).execute
    )
    return f"Deleted reaction {reaction_name}."


# --- Direct messages ---------------------------------------------------------


async def _find_direct_message(service, user: str) -> str:
    """Call spaces.findDirectMessage, mapping NOT_FOUND to a 'does not exist' answer."""
    user_name = _user_resource_name(user)
    try:
        space = await asyncio.to_thread(
            service.spaces().findDirectMessage(name=user_name).execute
        )
    except HttpError as e:
        if e.resp.status == 404:
            return (
                f"No direct message exists between you and {user_name} (exists: false). "
                "Use manage_chat_space(action='setup', space_type='DIRECT_MESSAGE', "
                f"members=['{user}']) to start one."
            )
        raise
    return (
        f"Direct message with {user_name} exists (exists: true). "
        f"Space ID: {space.get('name', '')}\n{_format_space(space)}"
    )


# --- Spaces ------------------------------------------------------------------


def _space_body(
    space_type: str,
    display_name: Optional[str],
    description: Optional[str],
    guidelines: Optional[str],
    external_user_allowed: Optional[bool],
) -> dict:
    """Build the Space resource for create/setup."""
    body = {"spaceType": space_type}
    if display_name:
        body["displayName"] = display_name
    details = {}
    if description is not None:
        details["description"] = description
    if guidelines is not None:
        details["guidelines"] = guidelines
    if details:
        body["spaceDetails"] = details
    if external_user_allowed is not None:
        body["externalUserAllowed"] = external_user_allowed
    return body


async def _create_space(
    service,
    space_type: str,
    display_name: Optional[str],
    description: Optional[str],
    guidelines: Optional[str],
    external_user_allowed: Optional[bool],
) -> str:
    """Call spaces.create for a named space."""
    if space_type == "GROUP_CHAT":
        raise UserInputError(
            "Google Chat only creates GROUP_CHAT spaces together with their initial "
            "members. Use action='setup' with space_type='GROUP_CHAT' and 'members'."
        )
    display_name = _require_arg(display_name, "display_name", "create")
    body = _space_body(
        space_type, display_name, description, guidelines, external_user_allowed
    )
    space = await asyncio.to_thread(service.spaces().create(body=body).execute)
    return f"Created space.\n{_format_space(space)}"


async def _setup_space(
    service,
    space_type: str,
    display_name: Optional[str],
    description: Optional[str],
    guidelines: Optional[str],
    external_user_allowed: Optional[bool],
    members: Optional[List[str]],
) -> str:
    """Call spaces.setup with initial human members (emails or users/ID)."""
    members = [m.strip() for m in (members or []) if m and m.strip()]
    if space_type == "SPACE":
        display_name = _require_arg(display_name, "display_name", "setup")
    elif display_name:
        raise UserInputError(
            f"'display_name' is not allowed for a {space_type}; only SPACE spaces are named."
        )
    if space_type == "DIRECT_MESSAGE" and len(members) != 1:
        raise UserInputError(
            "A DIRECT_MESSAGE setup needs exactly one entry in 'members'."
        )
    if space_type == "GROUP_CHAT" and not members:
        raise UserInputError(
            "A GROUP_CHAT setup needs at least one entry in 'members'."
        )
    if len(members) > _SETUP_MAX_MEMBERS:
        raise UserInputError(
            f"'members' may list at most {_SETUP_MAX_MEMBERS} people besides yourself."
        )
    space = _space_body(
        space_type, display_name, description, guidelines, external_user_allowed
    )
    if space_type == "DIRECT_MESSAGE":
        space["singleUserBotDm"] = False
    body = {"space": space}
    if members:
        body["memberships"] = [
            {"member": {"name": _user_resource_name(m), "type": "HUMAN"}}
            for m in members
        ]
    created = await asyncio.to_thread(service.spaces().setup(body=body).execute)
    return (
        f"Set up space with {len(members)} initial member(s) besides you.\n"
        f"{_format_space(created)}"
    )


async def _update_space(
    service,
    space_id: str,
    display_name: Optional[str],
    description: Optional[str],
    guidelines: Optional[str],
    history_state: Optional[str],
) -> str:
    """Call spaces.patch with an update mask built from the supplied fields."""
    mask = []
    body = {}
    if display_name is not None:
        mask.append("display_name")
        body["displayName"] = display_name
    if description is not None or guidelines is not None:
        # The API requires both SpaceDetails fields; keep the one not supplied.
        if description is None or guidelines is None:
            current = await asyncio.to_thread(
                service.spaces().get(name=space_id).execute
            )
            existing = current.get("spaceDetails") or {}
            if description is None:
                description = existing.get("description", "")
            if guidelines is None:
                guidelines = existing.get("guidelines", "")
        mask.append("space_details")
        body["spaceDetails"] = {"description": description, "guidelines": guidelines}
    if history_state is not None:
        mask.append("space_history_state")
        body["spaceHistoryState"] = history_state
    if not mask:
        raise UserInputError(
            "The 'update' action needs at least one of display_name, description, "
            "guidelines, or history_state."
        )
    space = await asyncio.to_thread(
        service.spaces()
        .patch(name=space_id, updateMask=",".join(mask), body=body)
        .execute
    )
    return f"Updated space (fields: {', '.join(mask)}).\n{_format_space(space)}"


async def _delete_space(service, space_id: str) -> str:
    """Call spaces.delete. Removes every message, membership, and attachment."""
    await asyncio.to_thread(service.spaces().delete(name=space_id).execute)
    return f"Deleted space {space_id} and everything in it."


async def _search_spaces(
    service, query: str, space_type: Optional[str], page_size: int
) -> str:
    """List the caller's spaces and filter display names client-side.

    spaces.search needs Workspace admin privileges, so this walks spaces.list
    (joined spaces only) and matches the query against displayName.
    """
    needle = query.strip().lower()
    params = {"pageSize": page_size}
    if space_type:
        params["filter"] = f'spaceType = "{space_type}"'
    matches = []
    scanned = 0
    for _ in range(_SEARCH_SPACES_MAX_PAGES):
        response = await asyncio.to_thread(service.spaces().list(**params).execute)
        spaces = response.get("spaces", [])
        scanned += len(spaces)
        matches.extend(
            s for s in spaces if needle in (s.get("displayName") or "").lower()
        )
        if not response.get("nextPageToken"):
            break
        params["pageToken"] = response["nextPageToken"]
    if not matches:
        return (
            f"No spaces with a display name containing '{query}' among the "
            f"{scanned} spaces you have joined."
        )
    output = [
        f"Found {len(matches)} space(s) with a display name containing '{query}' "
        f"(searched {scanned} joined spaces; organization-wide search needs admin access):"
    ]
    for s in matches:
        output.append(
            f"- {s.get('displayName')} (ID: {s.get('name', '')}, Type: {s.get('spaceType', 'UNKNOWN')})"
        )
    return "\n".join(output)
