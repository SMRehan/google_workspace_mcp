"""Unit tests for list_meet_conference_records and get_meet_conference_artifacts
against a Mock Meet service: filter forwarding, record lookup, every artifact
kind's resource path, paging and formatting."""

import os
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from core.utils import UserInputError
from gmeet.meet_tools import get_meet_conference_artifacts, list_meet_conference_records

EMAIL = "user@example.com"
RECORD = "conferenceRecords/rec-1"


def _unwrap(tool):
    """Unwrap FunctionTool + decorators to the original async function."""
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _record(name=RECORD, ended=True):
    record = {
        "name": name,
        "space": "spaces/jQCFfuBOdN5z",
        "startTime": "2026-01-05T09:00:00Z",
    }
    if ended:
        record["endTime"] = "2026-01-05T09:45:00Z"
    return record


def _records_service(records=None, next_page_token=None, single=None):
    response = {"conferenceRecords": records if records is not None else [_record()]}
    if next_page_token is not None:
        response["nextPageToken"] = next_page_token
    service = Mock()
    service.conferenceRecords().list().execute = Mock(return_value=response)
    service.conferenceRecords().get().execute = Mock(return_value=single or _record())
    service.conferenceRecords().list.reset_mock()
    service.conferenceRecords().get.reset_mock()
    return service


async def _list_records(service, **kwargs):
    return await _unwrap(list_meet_conference_records)(
        service=service, user_google_email=EMAIL, **kwargs
    )


class TestListConferenceRecords:
    @pytest.mark.asyncio
    async def test_no_filters_sends_no_filter_parameter(self):
        service = _records_service()

        result = await _list_records(service)

        assert service.conferenceRecords().list.call_args.kwargs == {}
        assert "Conference records for user@example.com (1 on this page):" in result
        assert "- Record: conferenceRecords/rec-1 (ended)" in result
        assert "  - Start: 2026-01-05 09:00:00 UTC" in result
        assert "  - End: 2026-01-05 09:45:00 UTC" in result
        assert "Next page token" not in result

    @pytest.mark.asyncio
    async def test_filters_are_combined_and_forwarded(self):
        service = _records_service()

        result = await _list_records(
            service,
            meeting_code="https://meet.google.com/abc-mnop-xyz",
            start_time_min="2026-01-01T00:00:00Z",
            end_time_max="2026-01-31T00:00:00Z",
        )

        expected = (
            'space.meeting_code = "abc-mnop-xyz" AND '
            'start_time>="2026-01-01T00:00:00Z" AND '
            'end_time<="2026-01-31T00:00:00Z"'
        )
        assert service.conferenceRecords().list.call_args.kwargs == {"filter": expected}
        assert f"matching: {expected}" in result

    @pytest.mark.asyncio
    async def test_space_name_filter_is_normalized(self):
        service = _records_service()

        await _list_records(service, space_name="jQCFfuBOdN5z")

        assert service.conferenceRecords().list.call_args.kwargs == {
            "filter": 'space.name = "spaces/jQCFfuBOdN5z"'
        }

    @pytest.mark.asyncio
    async def test_space_name_and_meeting_code_together_are_rejected(self):
        service = _records_service()

        with pytest.raises(UserInputError, match="not both"):
            await _list_records(
                service, space_name="spaces/a", meeting_code="abc-mnop-xyz"
            )

        service.conferenceRecords().list.assert_not_called()

    @pytest.mark.asyncio
    async def test_paging_is_forwarded_and_surfaced(self):
        service = _records_service(next_page_token="tok-2")

        result = await _list_records(service, page_size=5, page_token="tok-1")

        assert service.conferenceRecords().list.call_args.kwargs == {
            "pageSize": 5,
            "pageToken": "tok-1",
        }
        assert "Next page token: tok-2" in result

    @pytest.mark.asyncio
    async def test_empty_page_with_more_behind_it(self):
        service = _records_service(records=[], next_page_token="tok-2")

        result = await _list_records(service)

        assert "No conference records found on this page, but more pages remain." in (
            result
        )

    @pytest.mark.asyncio
    async def test_genuinely_empty(self):
        service = _records_service(records=[])

        result = await _list_records(service)

        assert "No conference records found." in result

    @pytest.mark.asyncio
    async def test_active_record_is_marked(self):
        service = _records_service(records=[_record(ended=False)])

        result = await _list_records(service)

        assert "- Record: conferenceRecords/rec-1 (active)" in result
        assert "  - End: in progress" in result


class TestGetConferenceRecord:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("conference_record", ["rec-1", RECORD])
    async def test_fetches_one_record_by_bare_id_or_full_name(self, conference_record):
        service = _records_service()

        result = await _list_records(service, conference_record=conference_record)

        assert service.conferenceRecords().get.call_args.kwargs == {"name": RECORD}
        service.conferenceRecords().list.assert_not_called()
        assert "Conference record for user@example.com:" in result
        assert "- Record: conferenceRecords/rec-1 (ended)" in result

    @pytest.mark.asyncio
    async def test_get_ignores_list_filters(self):
        service = _records_service()

        await _list_records(
            service, conference_record="rec-1", meeting_code="abc-mnop-xyz"
        )

        service.conferenceRecords().list.assert_not_called()
        service.conferenceRecords().get.assert_called_once()


def _artifact_service():
    service = Mock()
    return service


def _set_response(request_mock, items_key, items, next_page_token=None):
    response = {items_key: items}
    if next_page_token is not None:
        response["nextPageToken"] = next_page_token
    request_mock().execute = Mock(return_value=response)
    request_mock.reset_mock()


async def _artifacts(service, **kwargs):
    kwargs.setdefault("conference_record", "rec-1")
    return await _unwrap(get_meet_conference_artifacts)(
        service=service, user_google_email=EMAIL, **kwargs
    )


class TestParticipants:
    @pytest.mark.asyncio
    async def test_lists_participants_with_each_person_type(self):
        service = _artifact_service()
        participants_api = service.conferenceRecords().participants()
        _set_response(
            participants_api.list,
            "participants",
            [
                {
                    "name": f"{RECORD}/participants/p1",
                    "signedinUser": {"displayName": "Ada", "user": "users/42"},
                    "earliestStartTime": "2026-01-05T09:00:00Z",
                    "latestEndTime": "2026-01-05T09:45:00Z",
                },
                {
                    "name": f"{RECORD}/participants/p2",
                    "anonymousUser": {"displayName": "Guest"},
                    "earliestStartTime": "2026-01-05T09:01:00Z",
                },
                {
                    "name": f"{RECORD}/participants/p3",
                    "phoneUser": {"displayName": "+1 555"},
                    "earliestStartTime": "2026-01-05T09:02:00Z",
                },
            ],
        )

        result = await _artifacts(service, kind="participants")

        assert participants_api.list.call_args.kwargs == {"parent": RECORD}
        assert f"Participants of {RECORD} for user@example.com (3 on this page):" in (
            result
        )
        assert "- Ada (signed in, users/42)" in result
        assert "  - Left: 2026-01-05 09:45:00 UTC" in result
        assert "- Guest (anonymous)" in result
        assert "- +1 555 (phone)" in result
        assert "  - Left: still in call" in result

    @pytest.mark.asyncio
    async def test_forwards_paging(self):
        service = _artifact_service()
        participants_api = service.conferenceRecords().participants()
        _set_response(participants_api.list, "participants", [], next_page_token="t2")

        result = await _artifacts(
            service, kind="participants", page_size=50, page_token="t1"
        )

        assert participants_api.list.call_args.kwargs == {
            "parent": RECORD,
            "pageSize": 50,
            "pageToken": "t1",
        }
        assert "No participants found on this page, but more pages remain." in result
        assert "Next page token: t2" in result


class TestParticipantSessions:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("participant", ["p1", f"{RECORD}/participants/p1"])
    async def test_lists_sessions_under_the_participant(self, participant):
        service = _artifact_service()
        sessions_api = service.conferenceRecords().participants().participantSessions()
        _set_response(
            sessions_api.list,
            "participantSessions",
            [
                {
                    "name": f"{RECORD}/participants/p1/participantSessions/s1",
                    "startTime": "2026-01-05T09:00:00Z",
                    "endTime": "2026-01-05T09:20:00Z",
                },
                {
                    "name": f"{RECORD}/participants/p1/participantSessions/s2",
                    "startTime": "2026-01-05T09:25:00Z",
                },
            ],
        )

        result = await _artifacts(
            service, kind="participant_sessions", participant=participant
        )

        assert sessions_api.list.call_args.kwargs == {
            "parent": f"{RECORD}/participants/p1"
        }
        assert f"Sessions of {RECORD}/participants/p1" in result
        assert "2026-01-05 09:00:00 UTC -> 2026-01-05 09:20:00 UTC" in result
        assert "2026-01-05 09:25:00 UTC -> ongoing" in result

    @pytest.mark.asyncio
    async def test_requires_participant(self):
        service = _artifact_service()

        with pytest.raises(UserInputError, match="'participant' is required"):
            await _artifacts(service, kind="participant_sessions")


class TestRecordings:
    @pytest.mark.asyncio
    async def test_lists_recordings_with_drive_destinations(self):
        service = _artifact_service()
        recordings_api = service.conferenceRecords().recordings()
        _set_response(
            recordings_api.list,
            "recordings",
            [
                {
                    "name": f"{RECORD}/recordings/r1",
                    "state": "FILE_GENERATED",
                    "startTime": "2026-01-05T09:00:00Z",
                    "endTime": "2026-01-05T09:45:00Z",
                    "driveDestination": {
                        "file": "file-id",
                        "exportUri": "https://drive.google.com/file/d/file-id/view",
                    },
                }
            ],
        )

        result = await _artifacts(service, kind="recordings")

        assert recordings_api.list.call_args.kwargs == {"parent": RECORD}
        assert f"- Recording: {RECORD}/recordings/r1" in result
        assert "  - State: FILE_GENERATED" in result
        assert "  - Drive file: file-id" in result
        assert "  - Export URI: https://drive.google.com/file/d/file-id/view" in result


class TestTranscripts:
    @pytest.mark.asyncio
    async def test_lists_transcripts_with_docs_destinations(self):
        service = _artifact_service()
        transcripts_api = service.conferenceRecords().transcripts()
        _set_response(
            transcripts_api.list,
            "transcripts",
            [
                {
                    "name": f"{RECORD}/transcripts/t1",
                    "state": "FILE_GENERATED",
                    "docsDestination": {
                        "document": "doc-id",
                        "exportUri": "https://docs.google.com/document/d/doc-id",
                    },
                }
            ],
        )

        result = await _artifacts(service, kind="transcripts")

        assert transcripts_api.list.call_args.kwargs == {"parent": RECORD}
        assert f"- Transcript: {RECORD}/transcripts/t1" in result
        assert "  - Google Doc: doc-id" in result
        assert "  - Export URI: https://docs.google.com/document/d/doc-id" in result


class TestTranscriptEntries:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("transcript", ["t1", f"{RECORD}/transcripts/t1"])
    async def test_lists_entries_under_the_transcript(self, transcript):
        service = _artifact_service()
        entries_api = service.conferenceRecords().transcripts().entries()
        _set_response(
            entries_api.list,
            "transcriptEntries",
            [
                {
                    "name": f"{RECORD}/transcripts/t1/entries/e1",
                    "participant": f"{RECORD}/participants/p1",
                    "text": "Good morning everyone.",
                    "languageCode": "en-US",
                    "startTime": "2026-01-05T09:00:03Z",
                }
            ],
            next_page_token="more",
        )

        result = await _artifacts(
            service, kind="transcript_entries", transcript=transcript, page_size=100
        )

        assert entries_api.list.call_args.kwargs == {
            "parent": f"{RECORD}/transcripts/t1",
            "pageSize": 100,
        }
        assert (
            f"- [2026-01-05 09:00:03 UTC] {RECORD}/participants/p1: "
            "Good morning everyone. [en-US]"
        ) in result
        assert "Next page token: more" in result

    @pytest.mark.asyncio
    async def test_requires_transcript(self):
        service = _artifact_service()

        with pytest.raises(UserInputError, match="'transcript' is required"):
            await _artifacts(service, kind="transcript_entries")


class TestSmartNotes:
    @pytest.mark.asyncio
    async def test_lists_smart_notes_with_their_doc(self):
        service = _artifact_service()
        notes_api = service.conferenceRecords().smartNotes()
        _set_response(
            notes_api.list,
            "smartNotes",
            [
                {
                    "name": f"{RECORD}/smartNotes/n1",
                    "state": "FILE_GENERATED",
                    "docsDestination": {"document": "notes-doc"},
                }
            ],
        )

        result = await _artifacts(service, kind="smart_notes")

        assert notes_api.list.call_args.kwargs == {"parent": RECORD}
        assert f"- Smart note: {RECORD}/smartNotes/n1" in result
        assert "  - Google Doc: notes-doc" in result

    @pytest.mark.asyncio
    async def test_genuinely_empty(self):
        service = _artifact_service()
        notes_api = service.conferenceRecords().smartNotes()
        _set_response(notes_api.list, "smartNotes", [])

        result = await _artifacts(service, kind="smart_notes")

        assert "No smart notes found." in result


class TestKindValidation:
    @pytest.mark.asyncio
    async def test_invalid_kind(self):
        service = _artifact_service()

        with pytest.raises(UserInputError, match="Invalid kind 'chat'"):
            await _artifacts(service, kind="chat")

    @pytest.mark.asyncio
    async def test_kind_is_case_insensitive(self):
        service = _artifact_service()
        recordings_api = service.conferenceRecords().recordings()
        _set_response(recordings_api.list, "recordings", [])

        await _artifacts(service, kind="Recordings")

        recordings_api.list.assert_called_once()

    @pytest.mark.asyncio
    async def test_requires_conference_record(self):
        service = _artifact_service()

        with pytest.raises(UserInputError, match="conference_record"):
            await _artifacts(service, kind="recordings", conference_record="")
