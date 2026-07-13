"""Conformance tests for the orchestrator-api contract.

Validates that HostClient correctly parses all canonical fixture responses
and maps them to the right typed data models and exceptions.

    The fixture JSON files are the source of truth for the contract shape.
Both sides of the contract (host and provider) must conform to these fixtures.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from spec_kitty_orchestrator.host.client import (
    ContractMismatchError,
    MissionNotFoundError,
    HostClient,
    TransitionRejectedError,
    PolicyValidationError,
)
from spec_kitty_orchestrator.host.models import (
    AcceptMissionData,
    AppendHistoryData,
    ContractVersionData,
    MissionStateData,
    ListReadyData,
    MergeData,
    ResolveWorkspaceData,
    StartImplData,
    StartReviewData,
    TransitionData,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> dict:
    """Load a fixture JSON file by name (without .json extension)."""
    path = FIXTURES_DIR / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _make_client(repo_root: Path | None = None) -> HostClient:
    """Create a HostClient pointed at a dummy repo root."""
    root = repo_root or Path("/tmp/test-repo")
    return HostClient(
        repo_root=root,
        actor="test-orchestrator",
        policy_json='{"orchestrator_id":"test","orchestrator_version":"0.1.0","agent_family":"claude","approval_mode":"supervised","sandbox_mode":"workspace_write","network_mode":"none","dangerous_flags":[]}',
    )


def _patch_call(client: HostClient, fixture_name: str):
    """Context manager: patch HostClient._call to return the given fixture."""
    fixture = _load_fixture(fixture_name)

    from spec_kitty_orchestrator.host.models import HostResponse

    mock_response = HostResponse(**fixture)

    return patch.object(client, "_call", return_value=mock_response)


# -- Envelope shape validation ------------------------------------------------


class TestEnvelopeShape:
    """All fixtures must have the 7 required envelope keys."""

    @pytest.mark.parametrize("fixture_name", [
        "contract_version_success",
        "mission_state_success",
        "list_ready_success",
        "start_implementation_success",
        "resolve_workspace_success",
        "start_review_success",
        "transition_success",
        "append_history_success",
        "accept_mission_success",
        "merge_mission_success",
        "error_policy_required",
        "error_transition_rejected",
        "error_mission_not_found",
        "error_contract_mismatch",
    ])
    def test_fixture_has_required_keys(self, fixture_name: str) -> None:
        """Every fixture must contain all 7 envelope keys."""
        data = _load_fixture(fixture_name)
        required = {
            "contract_version",
            "command",
            "timestamp",
            "correlation_id",
            "success",
            "error_code",
            "data",
        }
        assert required.issubset(data.keys()), (
            f"{fixture_name} missing keys: {required - data.keys()}"
        )

    @pytest.mark.parametrize("fixture_name", [
        "contract_version_success",
        "mission_state_success",
        "list_ready_success",
        "start_implementation_success",
        "resolve_workspace_success",
        "start_review_success",
        "transition_success",
        "append_history_success",
        "accept_mission_success",
        "merge_mission_success",
    ])
    def test_success_fixtures_have_correct_shape(self, fixture_name: str) -> None:
        """Success fixtures must have success=true and error_code=null."""
        data = _load_fixture(fixture_name)
        assert data["success"] is True
        assert data["error_code"] is None
        assert isinstance(data["data"], dict)

    @pytest.mark.parametrize("fixture_name", [
        "error_policy_required",
        "error_transition_rejected",
        "error_mission_not_found",
        "error_contract_mismatch",
    ])
    def test_error_fixtures_have_correct_shape(self, fixture_name: str) -> None:
        """Error fixtures must have success=false and a non-null error_code."""
        data = _load_fixture(fixture_name)
        assert data["success"] is False
        assert data["error_code"] is not None
        assert isinstance(data["error_code"], str)
        assert isinstance(data["data"], dict)


# -- contract-version ---------------------------------------------------------


class TestContractVersion:
    def test_parses_success_fixture(self) -> None:
        client = _make_client()
        with _patch_call(client, "contract_version_success"):
            result = client.contract_version()
        assert isinstance(result, ContractVersionData)
        assert result.api_version == "1.3.0"
        assert result.min_supported_provider_version == "0.1.0"

    def test_mismatch_raises_contract_mismatch_error(self) -> None:
        client = _make_client()

        with patch.object(client, "_call", side_effect=ContractMismatchError(
            "CONTRACT_VERSION_MISMATCH",
            "Provider requires contract >=2.0.0 but host offers 1.0.0",
        )):
            with pytest.raises(ContractMismatchError) as exc_info:
                client.contract_version()
        assert exc_info.value.error_code == "CONTRACT_VERSION_MISMATCH"


# -- mission-state ------------------------------------------------------------


class TestMissionState:
    def test_parses_success_fixture(self) -> None:
        client = _make_client()
        with _patch_call(client, "mission_state_success"):
            result = client.mission_state("099-test-feature")
        assert isinstance(result, MissionStateData)
        assert result.mission_slug == "099-test-feature"
        assert len(result.work_packages) == 2
        wp_ids = {wp.wp_id for wp in result.work_packages}
        assert "WP01" in wp_ids
        assert "WP02" in wp_ids

    def test_mission_not_found_raises(self) -> None:
        client = _make_client()
        with patch.object(client, "_call", side_effect=MissionNotFoundError(
            "MISSION_NOT_FOUND",
            "Mission 'nonexistent-feature' not found in kitty-specs/",
        )):
            with pytest.raises(MissionNotFoundError) as exc_info:
                client.mission_state("nonexistent-feature")
        assert exc_info.value.error_code == "MISSION_NOT_FOUND"


# -- list-ready ---------------------------------------------------------------


class TestListReady:
    def test_parses_success_fixture(self) -> None:
        client = _make_client()
        with _patch_call(client, "list_ready_success"):
            result = client.list_ready("099-test-feature")
        assert isinstance(result, ListReadyData)
        assert result.mission_slug == "099-test-feature"
        assert len(result.ready_work_packages) == 1
        wp = result.ready_work_packages[0]
        assert wp.wp_id == "WP01"
        assert wp.dependencies_satisfied is True
        assert wp.recommended_base is None

    def test_accepts_omitted_recommended_base(self) -> None:
        client = _make_client()
        fixture = _load_fixture("list_ready_success")
        fixture["data"]["ready_work_packages"][0].pop("recommended_base")

        from spec_kitty_orchestrator.host.models import HostResponse

        with patch.object(client, "_call", return_value=HostResponse(**fixture)):
            result = client.list_ready("099-test-feature")

        assert result.ready_work_packages[0].recommended_base is None


# -- Host invocation ----------------------------------------------------------


class TestHostInvocation:
    def test_call_does_not_append_json_flag(self) -> None:
        client = _make_client(repo_root=Path("/tmp/test-repo"))
        fixture = _load_fixture("contract_version_success")
        mock_result = MagicMock()
        mock_result.stdout = json.dumps(fixture)
        mock_result.stderr = ""
        mock_result.returncode = 0

        with patch(
            "spec_kitty_orchestrator.host.client.subprocess.run",
            return_value=mock_result,
        ) as run:
            client._call(["contract-version"])

        run.assert_called_once()
        cmd = run.call_args.args[0]
        assert cmd == ["spec-kitty", "orchestrator-api", "contract-version"]
        assert "--json" not in cmd

    def test_append_history_runs_from_primary_checkout(self) -> None:
        """append-history must execute from repo_root (the primary checkout).

        This is the positive form of the SAFE_COMMIT_PATH_POLICY fix: append-history
        commits a planning artifact (a WP prompt file), and spec-kitty refuses to do
        that from inside a worktree. So its subprocess cwd and SPECIFY_REPO_ROOT must
        be repo_root, never a worktree path. Unlike the worktree-absence regression
        test, this pins the behavioral contract directly, so it stays meaningful as
        the CLI's worktree handling evolves.
        """
        root = Path("/tmp/primary-checkout")
        client = _make_client(repo_root=root)
        fixture = _load_fixture("append_history_success")
        mock_result = MagicMock()
        mock_result.stdout = json.dumps(fixture)
        mock_result.stderr = ""
        mock_result.returncode = 0

        with patch(
            "spec_kitty_orchestrator.host.client.subprocess.run",
            return_value=mock_result,
        ) as run:
            client.append_history("099-test-feature", "WP01", "note")

        run.assert_called_once()
        assert run.call_args.kwargs["cwd"] == root
        assert run.call_args.kwargs["env"]["SPECIFY_REPO_ROOT"] == str(root)


# -- start-implementation -----------------------------------------------------


class TestStartImplementation:
    def test_parses_success_fixture(self) -> None:
        client = _make_client()
        with _patch_call(client, "start_implementation_success"):
            result = client.start_implementation("099-test-feature", "WP01")
        assert isinstance(result, StartImplData)
        assert result.mission_slug == "099-test-feature"
        assert result.wp_id == "WP01"
        assert result.from_lane == "planned"
        assert result.to_lane == "in_progress"
        assert result.policy_metadata_recorded is True
        assert result.no_op is False
        # Contract >= 1.1.0: workspace_path is the lane worktree (not per-WP).
        assert result.workspace_path.endswith("lane-a")
        assert result.prompt_path.endswith("WP01.md")

    def test_policy_required_raises(self) -> None:
        client = _make_client()
        with patch.object(client, "_call", side_effect=PolicyValidationError(
            "POLICY_METADATA_REQUIRED",
            "--policy is required for start-implementation",
        )):
            with pytest.raises(PolicyValidationError) as exc_info:
                client.start_implementation("099-test-feature", "WP01")
        assert exc_info.value.error_code == "POLICY_METADATA_REQUIRED"

    def test_idempotent_noop(self) -> None:
        """Duplicate start-implementation (same actor, already in_progress) returns no_op=true."""
        client = _make_client()
        fixture = _load_fixture("start_implementation_success")
        fixture["data"]["no_op"] = True
        fixture["data"]["from_lane"] = "in_progress"

        from spec_kitty_orchestrator.host.models import HostResponse

        with patch.object(client, "_call", return_value=HostResponse(**fixture)):
            result = client.start_implementation("099-test-feature", "WP01")
        assert result.no_op is True

    def test_parses_lane_fields_when_present(self) -> None:
        """Contract >= 1.1.0: lane WPs carry lane_id/lane_branch/lane_base_ref."""
        client = _make_client()
        fixture = _load_fixture("start_implementation_success")
        fixture["data"].update({
            "lane_id": "lane-a",
            "lane_branch": "kitty/mission-feat-01ABCDEF-lane-a",
            "lane_base_ref": "kitty/mission-feat-01ABCDEF",
        })

        from spec_kitty_orchestrator.host.models import HostResponse

        with patch.object(client, "_call", return_value=HostResponse(**fixture)):
            result = client.start_implementation("099-test-feature", "WP01")
        assert result.lane_id == "lane-a"
        assert result.lane_branch == "kitty/mission-feat-01ABCDEF-lane-a"
        assert result.lane_base_ref == "kitty/mission-feat-01ABCDEF"

    def test_lane_fields_default_none_when_absent(self) -> None:
        """Planning/non-lane WPs omit lane_* — must parse as None."""
        client = _make_client()
        fixture = _load_fixture("start_implementation_success")
        for key in ("lane_id", "lane_branch", "lane_base_ref"):
            fixture["data"].pop(key, None)

        from spec_kitty_orchestrator.host.models import HostResponse

        with patch.object(client, "_call", return_value=HostResponse(**fixture)):
            result = client.start_implementation("099-test-feature", "WP01")
        assert result.lane_id is None
        assert result.lane_branch is None
        assert result.lane_base_ref is None


# -- start-review -------------------------------------------------------------


class TestStartReview:
    def test_parses_success_fixture(self) -> None:
        client = _make_client()
        with _patch_call(client, "start_review_success"):
            result = client.start_review("099-test-feature", "WP01", review_ref="review-001")
        assert isinstance(result, StartReviewData)
        assert result.from_lane == "for_review"
        assert result.to_lane == "in_review"
        assert result.policy_metadata_recorded is True


# -- resolve-workspace -------------------------------------------------------


class TestResolveWorkspace:
    def test_parses_success_fixture(self) -> None:
        client = _make_client()
        with _patch_call(client, "resolve_workspace_success"):
            result = client.resolve_workspace("099-test-feature", "WP01")
        assert isinstance(result, ResolveWorkspaceData)
        assert result.mission_slug == "099-test-feature"
        assert result.wp_id == "WP01"
        assert result.workspace_path.endswith("lane-a")
        assert result.prompt_path.endswith("WP01.md")
        assert result.lane_id == "lane-a"
        assert result.lane_branch == "kitty/mission-099-test-feature-lane-a"
        assert result.lane_base_ref == "kitty/mission-099-test-feature"


# -- transition ----------------------------------------------------------------


class TestTransition:
    def test_parses_success_fixture(self) -> None:
        client = _make_client()
        with _patch_call(client, "transition_success"):
            result = client.transition("099-test-feature", "WP01", "for_review")
        assert isinstance(result, TransitionData)
        assert result.from_lane == "in_progress"
        assert result.to_lane == "for_review"

    def test_invalid_transition_raises(self) -> None:
        client = _make_client()
        with patch.object(client, "_call", side_effect=TransitionRejectedError(
            "TRANSITION_REJECTED",
            "WP WP01 is in 'planned', cannot transition to 'done'",
        )):
            with pytest.raises(TransitionRejectedError) as exc_info:
                client.transition("099-test-feature", "WP01", "done")
        assert exc_info.value.error_code == "TRANSITION_REJECTED"

    def test_sends_structured_review_result_and_done_evidence(self) -> None:
        client = _make_client()
        fixture = _load_fixture("transition_success")

        from spec_kitty_orchestrator.host.models import HostResponse

        with patch.object(
            client,
            "_call",
            return_value=HostResponse(**fixture),
        ) as call:
            client.transition(
                "099-test-feature",
                "WP01",
                "done",
                review_result_json='{"reviewer":"codex"}',
                evidence_json='{"review":{}}',
            )

        args = call.call_args.args[0]
        assert args[args.index("--review-result-json") + 1] == '{"reviewer":"codex"}'
        assert args[args.index("--evidence-json") + 1] == '{"review":{}}'

    def test_sends_attributed_forced_review_recovery(self) -> None:
        client = _make_client()
        fixture = _load_fixture("transition_success")

        from spec_kitty_orchestrator.host.models import HostResponse

        with patch.object(
            client,
            "_call",
            return_value=HostResponse(**fixture),
        ) as call:
            client.transition(
                "099-test-feature",
                "WP01",
                "for_review",
                force=True,
                note="reviewer interrupted; re-queuing",
            )

        args = call.call_args.args[0]
        assert "--force" in args
        assert args[args.index("--actor") + 1] == "test-orchestrator"
        assert args[args.index("--note") + 1] == "reviewer interrupted; re-queuing"
        assert "--policy" in args


# -- append-history ------------------------------------------------------------


class TestAppendHistory:
    def test_parses_success_fixture(self) -> None:
        client = _make_client()
        with _patch_call(client, "append_history_success"):
            result = client.append_history("099-test-feature", "WP01", "Test note")
        assert isinstance(result, AppendHistoryData)
        assert result.wp_id == "WP01"
        assert result.history_entry_id.startswith("hist-")


# -- accept-mission ------------------------------------------------------------


class TestAcceptMission:
    def test_parses_success_fixture(self) -> None:
        client = _make_client()
        with _patch_call(client, "accept_mission_success"):
            result = client.accept_mission("099-test-feature")
        assert isinstance(result, AcceptMissionData)
        assert result.accepted is True
        assert result.mode == "auto"
        assert result.accepted_at is not None


# -- merge-mission -------------------------------------------------------------


class TestMergeMission:
    def test_parses_success_fixture(self) -> None:
        client = _make_client()
        with _patch_call(client, "merge_mission_success"):
            result = client.merge_mission("099-test-feature")
        assert isinstance(result, MergeData)
        assert result.merged is True
        assert result.target_branch == "main"
        assert result.strategy == "merge"
        assert "WP01" in result.merged_wps
        assert "WP02" in result.merged_wps

    def test_merge_idempotent_replay(self) -> None:
        """Duplicate merge-mission calls return success (replay safety)."""
        client = _make_client()
        fixture = _load_fixture("merge_mission_success")

        from spec_kitty_orchestrator.host.models import HostResponse

        mock_resp = HostResponse(**fixture)
        call_count = 0

        def side_effect(args: list) -> HostResponse:
            nonlocal call_count
            call_count += 1
            return mock_resp

        with patch.object(client, "_call", side_effect=side_effect):
            r1 = client.merge_mission("099-test-feature")
            r2 = client.merge_mission("099-test-feature")

        assert r1.merged is True
        assert r2.merged is True
        assert call_count == 2


# -- Error code mapping --------------------------------------------------------


class TestErrorCodeMapping:
    """All error codes in fixtures map to the correct HostError subclass."""

    @pytest.mark.parametrize("error_code,expected_cls", [
        ("POLICY_METADATA_REQUIRED", PolicyValidationError),
        ("TRANSITION_REJECTED", TransitionRejectedError),
        ("MISSION_NOT_FOUND", MissionNotFoundError),
        ("CONTRACT_VERSION_MISMATCH", ContractMismatchError),
    ])
    def test_error_code_maps_to_exception(
        self, error_code: str, expected_cls: type
    ) -> None:
        from spec_kitty_orchestrator.host.client import _ERROR_CODE_MAP

        assert error_code in _ERROR_CODE_MAP
        assert _ERROR_CODE_MAP[error_code] is expected_cls


# -- Boundary check ------------------------------------------------------------


class TestBoundaryCheck:
    """Verify no specify_cli or spec_kitty_events imports anywhere in provider code."""

    def test_no_specify_cli_imports(self) -> None:
        """All provider modules must not import specify_cli or spec_kitty_events."""
        import ast
        import spec_kitty_orchestrator

        src_root = Path(spec_kitty_orchestrator.__file__).parent
        banned_prefixes = ("specify_cli", "spec_kitty_events")

        violations: list[str] = []
        for py_file in sorted(src_root.rglob("*.py")):
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source)
            rel = py_file.relative_to(src_root.parent)

            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        for prefix in banned_prefixes:
                            if alias.name.startswith(prefix):
                                violations.append(
                                    f"{rel}: imports {alias.name!r}"
                                )
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        for prefix in banned_prefixes:
                            if node.module.startswith(prefix):
                                violations.append(
                                    f"{rel}: imports from {node.module!r}"
                                )

        assert not violations, (
            "Provider modules must not import host-internal packages:\n"
            + "\n".join(f"  - {v}" for v in violations)
        )


# -- Contract version enforcement ----------------------------------------------


class TestContractVersionEnforcement:
    """HostClient.contract_version() must raise ContractMismatchError when
    the host version is below _MIN_CONTRACT_VERSION."""

    def test_older_host_version_raises(self) -> None:
        """If host reports an older version, provider must refuse to proceed."""
        client = _make_client()
        fixture = _load_fixture("contract_version_success")

        # Simulate host returning an older version than _MIN_CONTRACT_VERSION
        old_version_fixture = dict(fixture)
        old_version_fixture["data"] = dict(fixture["data"])
        old_version_fixture["data"]["api_version"] = "0.9.0"  # below 1.0.0

        from spec_kitty_orchestrator.host.models import HostResponse

        with patch.object(client, "_call", return_value=HostResponse(**old_version_fixture)):
            with pytest.raises(ContractMismatchError) as exc_info:
                client.contract_version()

        assert exc_info.value.error_code == "CONTRACT_VERSION_MISMATCH"
        assert "0.9.0" in str(exc_info.value)

    def test_matching_host_version_succeeds(self) -> None:
        """If host reports the exact minimum version, no error is raised."""
        client = _make_client()
        with _patch_call(client, "contract_version_success"):
            result = client.contract_version()
        assert result.api_version == "1.3.0"

    def test_newer_host_version_succeeds(self) -> None:
        """If host reports a newer version (same major), no error is raised."""
        client = _make_client()
        fixture = _load_fixture("contract_version_success")

        newer_fixture = dict(fixture)
        newer_fixture["data"] = dict(fixture["data"])
        newer_fixture["data"]["api_version"] = "1.4.0"

        from spec_kitty_orchestrator.host.models import HostResponse

        with patch.object(client, "_call", return_value=HostResponse(**newer_fixture)):
            result = client.contract_version()

        assert result.api_version == "1.4.0"
