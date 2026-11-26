# Google Meet Reference

Tool-by-tool parameter reference for the Google Meet service.

### manage_meet_space
Create, inspect, reconfigure a Google Meet space, or end its active conference.

| Parameter | Type | Required | Default | Notes |
|-----------|------|----------|---------|-------|
| user_google_email | string | yes |  | The user's Google email address. Required. |
| action | string | yes |  | One of "create", "get", "update", "end_active_conference". |
| space_name | string | no |  | Space to act on, as "spaces/{id}", a meeting code such as "abc-mnop-xyz" or a meet.google.com URL. Required for every action except "create". |
| access_type | string | no |  | Who can join without knocking: "OPEN", "TRUSTED" (same organisation or invited) or "RESTRICTED" (invited only). |
| entry_point_access | string | no |  | "ALL" or "CREATOR_APP_ONLY" (join only via the app that created the space). |
| moderation | string | no |  | Host management "ON" or "OFF". |
| chat_restriction | string | no |  | "HOSTS_ONLY" or "NO_RESTRICTION". Needs moderation ON. |
| present_restriction | string | no |  | "HOSTS_ONLY" or "NO_RESTRICTION". Needs moderation ON. |
| reaction_restriction | string | no |  | "HOSTS_ONLY" or "NO_RESTRICTION". Needs moderation ON. |
| default_join_as_viewer | string | no |  | "ON" makes non-hosts join as viewers. Needs moderation ON. |
| auto_recording | string | no |  | Auto-record every conference: "ON" or "OFF". |
| auto_transcription | string | no |  | Auto-transcribe: "ON" or "OFF". |
| auto_smart_notes | string | no |  | Auto-generate Gemini notes: "ON" or "OFF". |
| attendance_report | string | no |  | Generate attendance reports: "ON" or "OFF". |

### manage_meet_member
List, inspect, add, re-role or remove members of a Google Meet space.

| Parameter | Type | Required | Default | Notes |
|-----------|------|----------|---------|-------|
| user_google_email | string | yes |  | The user's Google email address. Required. |
| action | string | yes |  | One of "list", "get", "add", "update_role", "remove", "batch_update". |
| space_name | string | yes |  | The space, as "spaces/{id}", a meeting code or a meet.google.com URL. Required. |
| member_name | string | no |  | Member id or "spaces/{id}/members/{member}". Required for "get", "update_role" and "remove". |
| member_names | array | no |  | Member ids or resource names for "batch_update" (max 500); every member gets the same role. |
| email | string | no |  | Email of the person to add. Required for "add". |
| role | string | no |  | "COHOST" or "ROLE_UNSPECIFIED" (plain member). Optional for "add"; required for "update_role" and "batch_update". |
| page_size | integer | no |  | Members per page for "list". |
| page_token | string | no |  | Continuation token from a previous "list" page. |

### list_meet_conference_records
List past and active Google Meet conferences, or fetch one conference record.

| Parameter | Type | Required | Default | Notes |
|-----------|------|----------|---------|-------|
| user_google_email | string | yes |  | The user's Google email address. Required. |
| conference_record | string | no |  | "conferenceRecords/{id}" or the bare id. When given, that record is returned and every other filter is ignored. |
| space_name | string | no |  | Only conferences held in this space ("spaces/{id}"). Mutually exclusive with meeting_code. |
| meeting_code | string | no |  | Only conferences held under this meeting code (e.g. "abc-mnop-xyz"). Mutually exclusive with space_name. |
| start_time_min | string | no |  | Only conferences that started at or after this RFC 3339 time, e.g. "2026-01-01T00:00:00Z". |
| end_time_max | string | no |  | Only conferences that ended at or before this RFC 3339 time. |
| page_size | integer | no |  | Records per page. |
| page_token | string | no |  | Continuation token from a previous page. |

### get_meet_conference_artifacts
List what a Google Meet conference produced: who attended, recordings,
transcripts, transcript text, or Gemini smart notes.

| Parameter | Type | Required | Default | Notes |
|-----------|------|----------|---------|-------|
| user_google_email | string | yes |  | The user's Google email address. Required. |
| conference_record | string | yes |  | "conferenceRecords/{id}" or the bare id. Find it with list_meet_conference_records. Required. |
| kind | string | yes |  | One of "participants", "participant_sessions", "recordings", "transcripts", "transcript_entries", "smart_notes". |
| participant | string | no |  | Participant id or full resource name. Required for kind "participant_sessions". |
| transcript | string | no |  | Transcript id or full resource name. Required for kind "transcript_entries". |
| page_size | integer | no |  | Items per page. |
| page_token | string | no |  | Continuation token from a previous page. |
