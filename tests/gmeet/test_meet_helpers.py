"""Unit tests for the pure helpers behind the Meet tools: resource-name
normalization, Space.config + updateMask construction, the conferenceRecords
filter builder and the response formatters."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from core.utils import UserInputError
from gmeet import meet_helpers as h


class TestNormalizeSpaceName:
    @pytest.mark.parametrize(
        "value, expected",
        [
            ("spaces/jQCFfuBOdN5z", "spaces/jQCFfuBOdN5z"),
            ("jQCFfuBOdN5z", "spaces/jQCFfuBOdN5z"),
            ("abc-mnop-xyz", "spaces/abc-mnop-xyz"),
            ("  spaces/abc-mnop-xyz  ", "spaces/abc-mnop-xyz"),
            ("https://meet.google.com/abc-mnop-xyz", "spaces/abc-mnop-xyz"),
            ("https://meet.google.com/abc-mnop-xyz?authuser=0", "spaces/abc-mnop-xyz"),
            ("http://meet.google.com/abc-mnop-xyz/", "spaces/abc-mnop-xyz"),
        ],
    )
    def test_accepts_names_codes_and_urls(self, value, expected):
        assert h.normalize_space_name(value) == expected

    def test_preserves_case_of_space_ids(self):
        """Space ids are case sensitive; only meeting codes are not."""
        assert h.normalize_space_name("spaces/AbCdEf") == "spaces/AbCdEf"

    @pytest.mark.parametrize("value", [None, "", "   ", "https://meet.google.com/"])
    def test_rejects_blank(self, value):
        with pytest.raises(UserInputError, match="space_name"):
            h.normalize_space_name(value)


class TestNormalizeMemberName:
    def test_full_resource_name_passes_through(self):
        name = "spaces/abc/members/m1"
        assert h.normalize_member_name(None, name) == name

    def test_bare_id_is_scoped_to_the_space(self):
        assert h.normalize_member_name("spaces/abc", "m1") == "spaces/abc/members/m1"

    def test_bare_id_with_meeting_code_space(self):
        assert (
            h.normalize_member_name("abc-mnop-xyz", "m1")
            == "spaces/abc-mnop-xyz/members/m1"
        )

    def test_bare_id_without_space_is_rejected(self):
        with pytest.raises(UserInputError, match="space_name"):
            h.normalize_member_name(None, "m1")

    @pytest.mark.parametrize("member", [None, "", "  "])
    def test_blank_member_is_rejected(self, member):
        with pytest.raises(UserInputError, match="member_name"):
            h.normalize_member_name("spaces/abc", member)


class TestNormalizeConferenceRecordName:
    def test_prefixes_bare_ids(self):
        assert h.normalize_conference_record_name("rec1") == "conferenceRecords/rec1"

    def test_keeps_full_names(self):
        assert (
            h.normalize_conference_record_name("conferenceRecords/rec1")
            == "conferenceRecords/rec1"
        )

    def test_rejects_blank(self):
        with pytest.raises(UserInputError, match="conference_record"):
            h.normalize_conference_record_name("  ")


class TestBuildSpaceConfig:
    def test_nothing_given_builds_nothing(self):
        assert h.build_space_config() == ({}, [])

    def test_every_field_lands_in_config_and_mask(self):
        config, mask = h.build_space_config(
            access_type="TRUSTED",
            entry_point_access="CREATOR_APP_ONLY",
            moderation="ON",
            chat_restriction="HOSTS_ONLY",
            present_restriction="NO_RESTRICTION",
            reaction_restriction="HOSTS_ONLY",
            default_join_as_viewer="ON",
            auto_recording="ON",
            auto_transcription="OFF",
            auto_smart_notes="ON",
            attendance_report="ON",
        )

        assert config == {
            "accessType": "TRUSTED",
            "entryPointAccess": "CREATOR_APP_ONLY",
            "moderation": "ON",
            "moderationRestrictions": {
                "chatRestriction": "HOSTS_ONLY",
                "presentRestriction": "NO_RESTRICTION",
                "reactionRestriction": "HOSTS_ONLY",
                "defaultJoinAsViewerType": "ON",
            },
            "artifactConfig": {
                "recordingConfig": {"autoRecordingGeneration": "ON"},
                "transcriptionConfig": {"autoTranscriptionGeneration": "OFF"},
                "smartNotesConfig": {"autoSmartNotesGeneration": "ON"},
            },
            "attendanceReportGenerationType": "GENERATE_REPORT",
        }
        assert mask == [
            "config.accessType",
            "config.entryPointAccess",
            "config.moderation",
            "config.moderationRestrictions.chatRestriction",
            "config.moderationRestrictions.presentRestriction",
            "config.moderationRestrictions.reactionRestriction",
            "config.moderationRestrictions.defaultJoinAsViewerType",
            "config.artifactConfig.recordingConfig.autoRecordingGeneration",
            "config.artifactConfig.transcriptionConfig.autoTranscriptionGeneration",
            "config.artifactConfig.smartNotesConfig.autoSmartNotesGeneration",
            "config.attendanceReportGenerationType",
        ]

    def test_only_given_fields_are_masked(self):
        config, mask = h.build_space_config(auto_recording="OFF")

        assert config == {
            "artifactConfig": {"recordingConfig": {"autoRecordingGeneration": "OFF"}}
        }
        assert mask == ["config.artifactConfig.recordingConfig.autoRecordingGeneration"]

    def test_attendance_report_off_maps_to_do_not_generate(self):
        config, _ = h.build_space_config(attendance_report="off")
        assert config["attendanceReportGenerationType"] == "DO_NOT_GENERATE"

    def test_values_are_case_insensitive(self):
        config, _ = h.build_space_config(access_type="open", moderation=" on ")
        assert config == {"accessType": "OPEN", "moderation": "ON"}

    @pytest.mark.parametrize(
        "kwargs, field",
        [
            ({"access_type": "PUBLIC"}, "access_type"),
            ({"entry_point_access": "NOBODY"}, "entry_point_access"),
            ({"moderation": "MAYBE"}, "moderation"),
            ({"chat_restriction": "ON"}, "chat_restriction"),
            ({"present_restriction": "OFF"}, "present_restriction"),
            ({"reaction_restriction": "NONE"}, "reaction_restriction"),
            ({"default_join_as_viewer": "YES"}, "default_join_as_viewer"),
            ({"auto_recording": "ALWAYS"}, "auto_recording"),
            ({"auto_transcription": "1"}, "auto_transcription"),
            ({"auto_smart_notes": "TRUE"}, "auto_smart_notes"),
            ({"attendance_report": "GENERATE_REPORT"}, "attendance_report"),
        ],
    )
    def test_invalid_values_are_rejected(self, kwargs, field):
        with pytest.raises(UserInputError, match=f"Invalid {field}"):
            h.build_space_config(**kwargs)


class TestBuildConferenceRecordsFilter:
    def test_no_criteria_gives_none(self):
        assert h.build_conference_records_filter() is None

    def test_space_name_is_normalized(self):
        assert (
            h.build_conference_records_filter(space_name="abc123")
            == 'space.name = "spaces/abc123"'
        )

    @pytest.mark.parametrize(
        "code",
        [
            "abc-mnop-xyz",
            "spaces/abc-mnop-xyz",
            "https://meet.google.com/abc-mnop-xyz",
        ],
    )
    def test_meeting_code_is_reduced_to_the_bare_code(self, code):
        assert (
            h.build_conference_records_filter(meeting_code=code)
            == 'space.meeting_code = "abc-mnop-xyz"'
        )

    def test_times_are_joined_with_and_in_a_stable_order(self):
        result = h.build_conference_records_filter(
            meeting_code="abc-mnop-xyz",
            start_time_min="2026-01-01T00:00:00Z",
            end_time_max="2026-01-31T23:59:59Z",
        )
        assert result == (
            'space.meeting_code = "abc-mnop-xyz" AND '
            'start_time>="2026-01-01T00:00:00Z" AND '
            'end_time<="2026-01-31T23:59:59Z"'
        )

    def test_space_name_and_meeting_code_are_mutually_exclusive(self):
        with pytest.raises(UserInputError, match="not both"):
            h.build_conference_records_filter(
                space_name="spaces/a", meeting_code="abc-mnop-xyz"
            )

    @pytest.mark.parametrize(
        "value", ["yesterday", "2026-01-01", "2026-13-01T00:00:00Z"]
    )
    def test_bad_timestamps_are_rejected(self, value):
        with pytest.raises(UserInputError, match="start_time_min"):
            h.build_conference_records_filter(start_time_min=value)
        with pytest.raises(UserInputError, match="end_time_max"):
            h.build_conference_records_filter(end_time_max=value)

    @pytest.mark.parametrize("value", ["   ", "https://meet.google.com/"])
    def test_blank_meeting_code_is_rejected(self, value):
        with pytest.raises(UserInputError, match="meeting_code"):
            h.build_conference_records_filter(meeting_code=value)


class TestFormatTime:
    def test_utc(self):
        assert h.format_time("2026-03-04T05:06:07.123456Z") == "2026-03-04 05:06:07 UTC"

    def test_other_offset_is_kept(self):
        assert h.format_time("2026-03-04T05:06:07+02:00") == "2026-03-04 05:06:07 +0200"

    def test_missing(self):
        assert h.format_time(None) == "N/A"
        assert h.format_time("") == "N/A"

    def test_unparseable_passes_through(self):
        assert h.format_time("soon") == "soon"


class TestFormatPerson:
    def test_signed_in_user(self):
        person = {"signedinUser": {"displayName": "Ada", "user": "users/42"}}
        assert h.format_person(person) == "Ada (signed in, users/42)"

    def test_anonymous_user(self):
        assert h.format_person({"anonymousUser": {"displayName": "Guest"}}) == (
            "Guest (anonymous)"
        )

    def test_phone_user(self):
        assert h.format_person({"phoneUser": {"displayName": "+1 555"}}) == (
            "+1 555 (phone)"
        )

    def test_unknown(self):
        assert h.format_person({}) == "Unknown participant"


class TestFormatters:
    def test_format_space_shows_identity_config_and_active_conference(self):
        text = h.format_space(
            {
                "name": "spaces/abc",
                "meetingCode": "abc-mnop-xyz",
                "meetingUri": "https://meet.google.com/abc-mnop-xyz",
                "activeConference": {"conferenceRecord": "conferenceRecords/r1"},
                "config": {
                    "accessType": "TRUSTED",
                    "moderation": "ON",
                    "moderationRestrictions": {"chatRestriction": "HOSTS_ONLY"},
                    "artifactConfig": {
                        "recordingConfig": {"autoRecordingGeneration": "ON"}
                    },
                    "attendanceReportGenerationType": "GENERATE_REPORT",
                },
                "phoneAccess": [
                    {"phoneNumber": "+1 555 0100", "pin": "123", "regionCode": "US"},
                    {"phoneNumber": "+44 20 0000", "regionCode": "GB"},
                ],
            }
        )

        assert "- Space: spaces/abc" in text
        assert "- Meeting code: abc-mnop-xyz" in text
        assert "- Meeting URI: https://meet.google.com/abc-mnop-xyz" in text
        assert "- Active conference: conferenceRecords/r1" in text
        assert "  - Access type: TRUSTED" in text
        assert "  - Moderation: ON" in text
        assert "  - Chat: HOSTS_ONLY" in text
        assert "  - Auto recording: ON" in text
        assert "  - Attendance report: GENERATE_REPORT" in text
        assert text.count("- Phone access:") == 1
        assert "  - +1 555 0100 PIN 123 (US)" in text
        assert "  - +44 20 0000 (GB)" in text

    def test_format_space_without_active_conference(self):
        text = h.format_space({"name": "spaces/abc"})
        assert "- Active conference: none" in text
        assert "Config" not in text

    def test_format_member(self):
        member = {"name": "spaces/a/members/m1", "email": "x@y.z", "role": "COHOST"}
        assert h.format_member(member) == "- x@y.z | role: COHOST | spaces/a/members/m1"

    def test_format_conference_record_active_and_ended(self):
        active = h.format_conference_record(
            {
                "name": "conferenceRecords/r1",
                "space": "spaces/abc",
                "startTime": "2026-01-01T10:00:00Z",
            }
        )
        assert "(active)" in active
        assert "  - End: in progress" in active

        ended = h.format_conference_record(
            {
                "name": "conferenceRecords/r1",
                "space": "spaces/abc",
                "startTime": "2026-01-01T10:00:00Z",
                "endTime": "2026-01-01T11:00:00Z",
                "expireTime": "2026-02-01T11:00:00Z",
            }
        )
        assert "(ended)" in ended
        assert "  - End: 2026-01-01 11:00:00 UTC" in ended
        assert "  - Expires: 2026-02-01 11:00:00 UTC" in ended

    def test_format_participant(self):
        text = h.format_participant(
            {
                "name": "conferenceRecords/r1/participants/p1",
                "signedinUser": {"displayName": "Ada", "user": "users/42"},
                "earliestStartTime": "2026-01-01T10:00:00Z",
            }
        )
        assert text.startswith("- Ada (signed in, users/42)")
        assert "  - Joined: 2026-01-01 10:00:00 UTC" in text
        assert "  - Left: still in call" in text

    def test_format_participant_session(self):
        text = h.format_participant_session(
            {
                "name": "conferenceRecords/r1/participants/p1/participantSessions/s1",
                "startTime": "2026-01-01T10:00:00Z",
            }
        )
        assert text.endswith("2026-01-01 10:00:00 UTC -> ongoing")

    def test_format_recording_with_drive_destination(self):
        text = h.format_recording(
            {
                "name": "conferenceRecords/r1/recordings/rec1",
                "state": "FILE_GENERATED",
                "startTime": "2026-01-01T10:00:00Z",
                "endTime": "2026-01-01T11:00:00Z",
                "driveDestination": {
                    "file": "drive-file-id",
                    "exportUri": "https://drive.google.com/file/d/drive-file-id/view",
                },
            }
        )
        assert "- Recording: conferenceRecords/r1/recordings/rec1" in text
        assert "  - State: FILE_GENERATED" in text
        assert "  - Drive file: drive-file-id" in text
        assert (
            "  - Export URI: https://drive.google.com/file/d/drive-file-id/view" in text
        )

    def test_format_transcript_with_docs_destination(self):
        text = h.format_transcript(
            {
                "name": "conferenceRecords/r1/transcripts/t1",
                "state": "FILE_GENERATED",
                "docsDestination": {
                    "document": "doc-id",
                    "exportUri": "https://docs.google.com/document/d/doc-id",
                },
            }
        )
        assert "  - Google Doc: doc-id" in text
        assert "  - Export URI: https://docs.google.com/document/d/doc-id" in text

    def test_format_transcript_entry(self):
        text = h.format_transcript_entry(
            {
                "participant": "conferenceRecords/r1/participants/p1",
                "text": " Hello there. ",
                "languageCode": "en-US",
                "startTime": "2026-01-01T10:00:05Z",
            }
        )
        assert text == (
            "- [2026-01-01 10:00:05 UTC] conferenceRecords/r1/participants/p1: "
            "Hello there. [en-US]"
        )

    def test_format_smart_note(self):
        text = h.format_smart_note(
            {
                "name": "conferenceRecords/r1/smartNotes/n1",
                "state": "FILE_GENERATED",
                "docsDestination": {"document": "notes-doc"},
            }
        )
        assert "- Smart note: conferenceRecords/r1/smartNotes/n1" in text
        assert "  - Google Doc: notes-doc" in text
        assert "Export URI" not in text


class TestFormatPage:
    def test_empty_page_with_more_behind_it_is_not_reported_as_empty(self):
        text = h._format_page("Header:", [], str, "No items found", "tok")
        assert "No items found on this page, but more pages remain." in text
        assert "Next page token: tok" in text

    def test_genuinely_empty_page(self):
        text = h._format_page("Header:", [], str, "No items found", None)
        assert text == "Header:\nNo items found."

    def test_items_are_formatted_in_order(self):
        text = h._format_page(
            "H:", [{"a": 1}, {"a": 2}], lambda i: str(i["a"]), "x", None
        )
        assert text == "H:\n1\n2"


class TestPagingParams:
    def test_omits_missing_values(self):
        assert h._paging_params(None, None) == {}
        assert h._paging_params(None, "") == {}

    def test_forwards_values(self):
        assert h._paging_params(10, "tok") == {"pageSize": 10, "pageToken": "tok"}

    def test_rejects_non_positive_page_size(self):
        with pytest.raises(UserInputError, match="page_size"):
            h._paging_params(0, None)
