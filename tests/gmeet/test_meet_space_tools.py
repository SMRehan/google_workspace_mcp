"""Unit tests for manage_meet_space: every action against a Mock Meet service,
plus the request bodies, updateMask and meeting-code normalization it sends."""

import os
import sys
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from core.utils import UserInputError
from gmeet.meet_tools import manage_meet_space

EMAIL = "user@example.com"


def _unwrap(tool):
    """Unwrap FunctionTool + decorators to the original async function."""
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _space(**overrides):
    space = {
        "name": "spaces/jQCFfuBOdN5z",
        "meetingCode": "abc-mnop-xyz",
        "meetingUri": "https://meet.google.com/abc-mnop-xyz",
        "config": {"accessType": "TRUSTED", "entryPointAccess": "ALL"},
    }
    space.update(overrides)
    return space


def _service(space=None):
    service = Mock()
    space = space or _space()
    service.spaces().create().execute = Mock(return_value=space)
    service.spaces().get().execute = Mock(return_value=space)
    service.spaces().patch().execute = Mock(return_value=space)
    service.spaces().endActiveConference().execute = Mock(return_value={})
    # Drop the priming calls made above so call_args reflects the tool's call.
    for method in ("create", "get", "patch", "endActiveConference"):
        getattr(service.spaces(), method).reset_mock()
    return service


async def _run(service, **kwargs):
    return await _unwrap(manage_meet_space)(
        service=service, user_google_email=EMAIL, **kwargs
    )


class TestCreate:
    @pytest.mark.asyncio
    async def test_create_without_config_sends_an_empty_body(self):
        service = _service()

        result = await _run(service, action="create")

        assert service.spaces().create.call_args.kwargs == {"body": {}}
        assert "Meet space created for user@example.com" in result
        assert "- Space: spaces/jQCFfuBOdN5z" in result
        assert "- Meeting code: abc-mnop-xyz" in result
        assert "- Meeting URI: https://meet.google.com/abc-mnop-xyz" in result

    @pytest.mark.asyncio
    async def test_create_with_config_nests_it_under_config(self):
        service = _service()

        await _run(
            service,
            action="create",
            access_type="RESTRICTED",
            moderation="ON",
            chat_restriction="HOSTS_ONLY",
            auto_transcription="ON",
            attendance_report="ON",
        )

        assert service.spaces().create.call_args.kwargs["body"] == {
            "config": {
                "accessType": "RESTRICTED",
                "moderation": "ON",
                "moderationRestrictions": {"chatRestriction": "HOSTS_ONLY"},
                "artifactConfig": {
                    "transcriptionConfig": {"autoTranscriptionGeneration": "ON"}
                },
                "attendanceReportGenerationType": "GENERATE_REPORT",
            }
        }

    @pytest.mark.asyncio
    async def test_create_rejects_invalid_config_before_calling_the_api(self):
        service = _service()

        with pytest.raises(UserInputError, match="Invalid access_type"):
            await _run(service, action="create", access_type="PUBLIC")

        service.spaces().create.assert_not_called()


class TestGet:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "space_name",
        ["spaces/abc-mnop-xyz", "abc-mnop-xyz", "https://meet.google.com/abc-mnop-xyz"],
    )
    async def test_get_normalizes_meeting_codes_and_urls(self, space_name):
        service = _service()

        result = await _run(service, action="get", space_name=space_name)

        assert service.spaces().get.call_args.kwargs == {"name": "spaces/abc-mnop-xyz"}
        assert "Meet space for user@example.com" in result

    @pytest.mark.asyncio
    async def test_get_reports_the_active_conference(self):
        service = _service(
            _space(activeConference={"conferenceRecord": "conferenceRecords/r1"})
        )

        result = await _run(service, action="get", space_name="spaces/x")

        assert "- Active conference: conferenceRecords/r1" in result

    @pytest.mark.asyncio
    async def test_get_without_space_name_is_rejected(self):
        service = _service()

        with pytest.raises(UserInputError, match="space_name"):
            await _run(service, action="get")

        service.spaces().get.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_with_config_fields_is_rejected(self):
        """Silently ignoring a requested setting change would mislead the caller."""
        service = _service()

        with pytest.raises(
            UserInputError, match="only accepted by 'create' and 'update'"
        ):
            await _run(service, action="get", space_name="spaces/x", moderation="ON")

        service.spaces().get.assert_not_called()


class TestUpdate:
    @pytest.mark.asyncio
    async def test_update_sends_only_the_given_fields_and_a_matching_mask(self):
        service = _service()

        result = await _run(
            service,
            action="update",
            space_name="abc-mnop-xyz",
            access_type="OPEN",
            auto_recording="OFF",
        )

        assert service.spaces().patch.call_args.kwargs == {
            "name": "spaces/abc-mnop-xyz",
            "updateMask": (
                "config.accessType,"
                "config.artifactConfig.recordingConfig.autoRecordingGeneration"
            ),
            "body": {
                "config": {
                    "accessType": "OPEN",
                    "artifactConfig": {
                        "recordingConfig": {"autoRecordingGeneration": "OFF"}
                    },
                }
            },
        }
        assert "Meet space updated for user@example.com" in result

    @pytest.mark.asyncio
    async def test_update_without_any_field_is_rejected(self):
        service = _service()

        with pytest.raises(UserInputError, match="at least one config field"):
            await _run(service, action="update", space_name="spaces/x")

        service.spaces().patch.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_without_space_name_is_rejected(self):
        service = _service()

        with pytest.raises(UserInputError, match="space_name"):
            await _run(service, action="update", moderation="OFF")

        service.spaces().patch.assert_not_called()


class TestEndActiveConference:
    @pytest.mark.asyncio
    async def test_calls_end_active_conference_with_an_empty_body(self):
        service = _service()

        result = await _run(
            service, action="end_active_conference", space_name="abc-mnop-xyz"
        )

        assert service.spaces().endActiveConference.call_args.kwargs == {
            "name": "spaces/abc-mnop-xyz",
            "body": {},
        }
        assert "Ended the active conference in spaces/abc-mnop-xyz" in result

    @pytest.mark.asyncio
    async def test_rejects_config_fields(self):
        service = _service()

        with pytest.raises(UserInputError, match="only accepted by"):
            await _run(
                service,
                action="end_active_conference",
                space_name="spaces/x",
                access_type="OPEN",
            )

        service.spaces().endActiveConference.assert_not_called()


class TestActionValidation:
    @pytest.mark.asyncio
    async def test_invalid_action(self):
        service = _service()

        with pytest.raises(UserInputError, match="Invalid action 'delete'"):
            await _run(service, action="delete", space_name="spaces/x")

    @pytest.mark.asyncio
    async def test_action_is_case_insensitive(self):
        service = _service()

        await _run(service, action="GET", space_name="spaces/x")

        service.spaces().get.assert_called_once()

    @pytest.mark.asyncio
    async def test_denied_action_under_granular_permissions(self):
        service = _service()

        with patch("gmeet.meet_tools.is_action_denied", return_value=True) as denied:
            with pytest.raises(UserInputError, match="not allowed under the current"):
                await _run(service, action="create")

        denied.assert_called_once_with("meet", "create")
        service.spaces().create.assert_not_called()
