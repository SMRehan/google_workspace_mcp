# Google Chat Tools Reference

MCP tools for Google Chat spaces, messages, reactions, and attachments. All tools require `user_google_email` (string, required).

---

## Spaces

### list_spaces
Lists Google Chat spaces (rooms and direct messages) accessible to the user, or the members of one space.

| Parameter | Type | Required | Default | Notes |
|-----------|------|----------|---------|-------|
| user_google_email | string | yes | | |
| page_size | integer | no | 100 | |
| space_type | string | no | "all" | |
| space_id | string | no | | List this space's members instead: people, apps and groups, with role and pending invites; at most `page_size` are shown. Needs `chat.memberships.readonly` (in the default Chat scopes; not available in service-account mode) |

---

## Messages

### get_messages
Retrieves one page of messages from a Google Chat space; use the `Next page token` from the response as `page_token` to retrieve more.

| Parameter | Type | Required | Default | Notes |
|-----------|------|----------|---------|-------|
| user_google_email | string | yes | | |
| space_id | string | yes | | |
| page_size | integer | no | 50 | |
| order_by | string | no | "createTime desc" | |
| message_filter | string | no | | Chat API filter on `createTime` and `thread.name`, e.g. `createTime > "2026-03-18T00:00:00Z"` or `thread.name = spaces/X/threads/Y` |
| page_token | string | no | | Token from a previous response's `Next page token`. Keep all other parameters unchanged between pages |

### search_messages
Searches for messages in Google Chat spaces by text content and/or time range.

| Parameter | Type | Required | Default | Notes |
|-----------|------|----------|---------|-------|
| user_google_email | string | yes | | |
| query | string | no | | Text to search for (case-insensitive substring, matched client-side); omit to apply only time_filter |
| time_filter | string | no | | Chat API `createTime` filter, e.g. `createTime > "2026-03-18T00:00:00Z"`; combine bounds with `AND` |
| space_id | string | no | | Filter to a specific space |
| page_size | integer | no | 25 | Max messages fetched per space |
| max_spaces | integer | no | 10 | Spaces searched when space_id is omitted; pages through the space list until this many are found |

### send_message
Sends a message to a Google Chat space. Can reply to existing threads, or edit a message already sent.

| Parameter | Type | Required | Default | Notes |
|-----------|------|----------|---------|-------|
| user_google_email | string | yes | | |
| space_id | string | yes | | |
| message_text | string | yes | | |
| thread_key | string | no | | App-defined key; creates thread if not found |
| thread_name | string | no | | Resource name, e.g. `spaces/X/threads/Y` |
| message_name | string | no | | Edit this message in place, e.g. `spaces/X/messages/Y`; must sit in `space_id`, own messages only, cannot be combined with a thread parameter |

---

## Reactions & Attachments

### create_reaction
Adds an emoji reaction to a message.

| Parameter | Type | Required | Default | Notes |
|-----------|------|----------|---------|-------|
| user_google_email | string | yes | | |
| message_id | string | yes | | Resource name, e.g. `spaces/X/messages/Y` |
| emoji_unicode | string | yes | | Literal Unicode emoji character (not a shortcode) |

### download_chat_attachment
Downloads an attachment from a Chat message. Returns a local file path (stdio mode) or a temporary URL valid for 1 hour (HTTP mode).

| Parameter | Type | Required | Default | Notes |
|-----------|------|----------|---------|-------|
| user_google_email | string | yes | | |
| message_id | string | yes | | Resource name, e.g. `spaces/X/messages/Y` |
| attachment_index | integer | no | 0 | Zero-based index of the attachment |

---

## Tips

**Space IDs**: Call `list_spaces` first to discover available spaces and their IDs. You need the space ID for all message operations.

**Unnamed spaces**: Direct messages and group chats have no name of their own, so `list_spaces`, `get_messages` and `search_messages` label them after up to three other members (e.g. `Alice Smith, Bob Jones and 2 others`). Naming needs the `chat.memberships.readonly` and `contacts.readonly` scopes (requested with the Chat scopes) and the People API enabled in the Google Cloud project. Tokens granted before `chat.memberships.readonly` was added keep working without re-consent. If the member lookup fails, for example because that scope is missing, the label falls back to `Direct message` or `Group chat`; if only a member's name cannot be resolved, their user ID (e.g. `users/123`) is shown in its place.

**Message resource names**: Messages are identified by their full resource name in the format `spaces/SPACE_ID/messages/MESSAGE_ID`. This is the value expected by `create_reaction` and `download_chat_attachment`.

**Reactions**: The `emoji_unicode` parameter takes a literal Unicode emoji character (e.g. a thumbs-up character), not an emoji shortcode.

### manage_chat_member
Lists, inspects, adds, re-roles, or removes members of a Google Chat space.

| Parameter | Type | Required | Default | Notes |
|-----------|------|----------|---------|-------|
| user_google_email | string | yes |  |  |
| action | string | yes |  | One of "list", "get", "add", "update_role", "remove". |
| space_id | string | no |  | Space (spaces/X or a bare ID X). Required for "list" and "add"; with 'member' it also identifies the membership for the other actions. |
| membership_name | string | no |  | Membership resource name (spaces/X/members/Y) for "get", "update_role", "remove". Alternatively pass space_id plus member. |
| member | string | no |  | The person as an email address or users/ID, or "app" for the calling Chat app. Required for "add"; identifies the membership together with space_id for "get",… |
| role | string | no |  | "ROLE_MEMBER" or "ROLE_MANAGER". Required for "update_role"; optional for "add" (a manager role is applied with a follow-up update). |
| member_type | string | no |  | "HUMAN" or "BOT". Filters "list"; sets the type for "add" (default HUMAN). |
| page_size | integer | no | 100 | Members per page for "list" (default 100). |
| page_token | string | no |  | Continue a previous "list" from its nextPageToken. |
| show_invited | boolean | no | false | For "list", also include invited-but-not-joined members. |

### manage_chat_message
Gets, edits, or deletes a single Google Chat message by resource name.

| Parameter | Type | Required | Default | Notes |
|-----------|------|----------|---------|-------|
| user_google_email | string | yes |  |  |
| action | string | yes |  | One of "get", "update", "delete". |
| message_name | string | yes |  | Message resource name (spaces/X/messages/Y), as returned by send_message or get_messages. |
| text | string | no |  | New message text for "update". |
| update_mask | string | no |  | Comma-separated field paths for "update" (default "text"). Supported: text, attachment, quoted_message_metadata (the last only removes the quoted message). |
| force | boolean | no | false | For "delete", also delete the message's threaded replies. Without it, deleting a message that has replies fails. |

### manage_chat_reaction
Lists the emoji reactions on a Google Chat message, or deletes one of yours.

| Parameter | Type | Required | Default | Notes |
|-----------|------|----------|---------|-------|
| user_google_email | string | yes |  |  |
| action | string | yes |  | One of "list", "delete". |
| message_name | string | no |  | Message resource name (spaces/X/messages/Y). Required for "list". |
| reaction_name | string | no |  | Reaction resource name (spaces/X/messages/Y/reactions/Z), as returned by "list" or create_reaction. Required for "delete". |
| emoji_unicode | string | no |  | For "list", only reactions with this emoji (e.g. 👍). |
| user | string | no |  | For "list", only reactions by this user (users/ID; an email alias may not be accepted by the filter). |
| page_size | integer | no | 25 | Reactions per page for "list" (default 25). |
| page_token | string | no |  | Continue a previous "list" from its nextPageToken. |

### find_chat_direct_message
Finds the existing direct message space between you and another user.

| Parameter | Type | Required | Default | Notes |
|-----------|------|----------|---------|-------|
| user_google_email | string | yes |  |  |
| user | string | yes |  | The other person's email address or users/ID. |

### manage_chat_space
Creates, sets up, updates, deletes, or searches Google Chat spaces.

| Parameter | Type | Required | Default | Notes |
|-----------|------|----------|---------|-------|
| user_google_email | string | yes |  |  |
| action | string | yes |  | One of "create", "setup", "update", "delete", "search". |
| space_id | string | no |  | Space (spaces/X or a bare ID X). Required for "update" and "delete". |
| display_name | string | no |  | Space name (up to 128 chars). Required to create/set up a SPACE; not allowed for GROUP_CHAT or DIRECT_MESSAGE. Updatable. |
| description | string | no |  | What the space is for (up to 4,096 chars). For create/setup/update. |
| guidelines | string | no |  | Space rules and etiquette (up to 5,000 chars). For create/setup/update. |
| space_type | string | no |  | "SPACE" (default) or "GROUP_CHAT" for "create"/"setup"; "setup" also accepts "DIRECT_MESSAGE" with exactly one member. Optionally narrows "search". GROUP_CHAT… |
| external_user_allowed | boolean | no |  | For create/setup, whether people outside your organization may join. Immutable afterwards. |
| history_state | string | no |  | For "update", "HISTORY_ON" or "HISTORY_OFF". |
| members | array | no |  | For "setup", up to 49 people (emails or users/ID) to add alongside you. Omit yourself. |
| query | string | no |  | For "search", case-insensitive text matched against display names of spaces you have joined (spaces.search is admin-only, so this walks spaces.list instead). |
| page_size | integer | no | 100 | For "search", spaces fetched per page (default 100). |
