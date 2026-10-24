"""Privacy and evidence locks for the opt-in paid analysis journey."""

import hashlib
import importlib.util
import json
import secrets
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def smoke():
    path = Path(__file__).resolve().parents[3] / "scripts/analysis_configured_provider_smoke.py"
    spec = importlib.util.spec_from_file_location("analysis_smoke_tests", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_chart_reference_must_be_in_assistant_message_not_tool_output(smoke):
    marker = secrets.token_hex(32)
    chart = "```analysis-chart\n" + json.dumps({
        "analysis_id": marker, "x_column": "category", "y_column": "value", "type": "bar",
    }) + "\n```"
    tool_frames = [(0, {"type": "message", "role": "tool", "content": [{"type": "text", "text": chart}]}),
                   (1, {"object": "response", "output": [{"role": "assistant", "type": "function_call_output",
                                                            "content": [{"type": "text", "text": chart}]}]})]
    assert smoke.chart_references(smoke.assistant_text(tool_frames)) == []
    assistant = [(2, {"type": "message", "role": "assistant", "content": [{"type": "text", "text": chart}]})]
    assert smoke.chart_references(smoke.assistant_text(assistant))[0]["analysis_id"] == marker


def test_model_chart_numbers_cannot_pass_reference_check(smoke):
    reference = {"analysis_id": secrets.token_hex(32), "x_column": "category", "y_column": "value", "type": "bar"}
    reference["data"] = [999]
    assert smoke.chart_references("```analysis-chart\n" + json.dumps(reference) + "\n```") == []


def test_pptx_verifier_reads_standard_and_multilevel_category_caches(smoke):
    from xml.etree import ElementTree

    chart = ElementTree.fromstring(
        """<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart">
        <c:chart><c:plotArea><c:barChart><c:ser>
          <c:cat><c:strRef><c:strCache><c:pt idx="0"><c:v>North</c:v></c:pt></c:strCache></c:strRef></c:cat>
          <c:cat><c:multiLvlStrRef><c:multiLvlStrCache><c:lvl><c:pt idx="0"><c:v>South</c:v></c:pt></c:lvl></c:multiLvlStrCache></c:multiLvlStrRef></c:cat>
        </c:ser></c:barChart></c:plotArea></c:chart>
        </c:chartSpace>"""
    )

    assert smoke.chart_category_labels([chart]) == {"North", "South"}


def test_record_checksum_is_independent_and_rejects_tampering(smoke, tmp_path):
    value = {"rows": [{"category": "East", "amount": "10.20"}]}
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    value["record_sha256"] = hashlib.sha256(canonical).hexdigest()
    path = tmp_path / "record.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    assert smoke.sealed_record(path)["rows"][0]["amount"] == "10.20"
    value["rows"][0]["amount"] = "99.20"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(smoke.SmokeFailure, match="^record_checksum_mismatch$"):
        smoke.sealed_record(path)


def test_source_backed_analysis_rejects_equal_values_with_wrong_filter(smoke, tmp_path):
    value = {"analysis_id": secrets.token_hex(32), "evidence": {"source_sha256": "abc"},
             "plan": {"group_by": ["region"], "filters": [{"column": "region", "op": "eq", "value": "East"}],
                      "metrics": [{"op": "sum", "column": "amount", "as": "total"}]},
             "rows": [{"region": "East", "total": "10.20"}]}
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    value["record_sha256"] = hashlib.sha256(canonical).hexdigest()
    path = tmp_path / "analysis" / "scope" / "results" / "result.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(smoke.SmokeFailure, match="^expected_source_backed_analysis_missing$"):
        smoke.matching_result(tmp_path, "abc", {"group_column": "region", "value_column": "amount"}, {"East": "10.20"})


def test_source_backed_analysis_uses_normalized_metric_as_field(smoke, tmp_path):
    value = {"analysis_id": secrets.token_hex(32), "evidence": {"source_sha256": "abc"},
             "plan": {"group_by": ["region"], "filters": [], "deduplicate_by": [],
                      "metrics": [{"op": "sum", "column": "amount", "as": "total"}]},
             "rows": [{"region": "East", "total": "10.20"}, {"region": "West", "total": "5.30"}]}
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    value["record_sha256"] = hashlib.sha256(canonical).hexdigest()
    path = tmp_path / "analysis" / "scope" / "results" / "result.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    record, alias = smoke.matching_result(tmp_path, "abc", {"group_column": "region", "value_column": "amount"},
                                         {"East": "10.20", "West": "5.30"})
    assert record["analysis_id"] == value["analysis_id"]
    assert alias == "total"


def test_only_expected_analysis_deliveries_can_be_auto_approved(smoke):
    assert smoke.DELIVERY_TOOLS == {"create_analysis_chart", "create_analysis_report",
                                    "create_analysis_presentation", "export_analysis_evidence",
                                    "get_presentation_generation_status"}
    assert "execute_shell_command" not in smoke.DELIVERY_TOOLS
    assert "clear_analysis_data" not in smoke.DELIVERY_TOOLS


def test_source_smoke_uses_the_repository_presentation_runtime(smoke):
    env = smoke.source_presentation_runtime_env()

    module = Path(env["LANTAI_PPTXGENJS_MODULE"])
    assert module == smoke.ROOT / "console" / "node_modules" / "pptxgenjs"
    assert module.is_dir()


def test_approval_diagnostics_are_scoped_and_never_include_arguments(smoke):
    item = {"root_session_id": "test", "user_id": "default", "agent_id": "default",
            "tool_name": "create_analysis_chart", "arguments": "private"}
    smoke.validate_approval(item, "test")
    for key, code in [("root_session_id", "approval_root_session_mismatch"),
                      ("user_id", "approval_user_mismatch"), ("agent_id", "approval_agent_mismatch")]:
        with pytest.raises(smoke.SmokeFailure, match=f"^{code}$"):
            smoke.validate_approval({**item, key: "private"}, "test")
    with pytest.raises(smoke.SmokeFailure, match="^unexpected_tool_approval_execute_shell_command$"):
        smoke.validate_approval({**item, "tool_name": "execute_shell_command"}, "test")
    with pytest.raises(smoke.SmokeFailure, match="^unexpected_tool_approval_other$"):
        smoke.validate_approval({**item, "tool_name": "private"}, "test")


def test_generic_delivery_requires_observed_exact_sealed_file(smoke, tmp_path):
    item = {"root_session_id": "test", "user_id": "default", "agent_id": "default",
            "tool_name": "send_file_to_user", "policy_decision": {}}
    with pytest.raises(smoke.SmokeFailure, match="^file_delivery_attestation_missing$"):
        smoke.validate_approval(item, "test")
    checksum = secrets.token_hex(32)
    item["policy_decision"]["analysis_smoke_final_file_sha256"] = checksum
    smoke.validate_approval(item, "test")
    with pytest.raises(smoke.SmokeFailure, match="^file_delivery_workspace_escape$"):
        smoke.validate_approval({**item, "policy_decision": {
            **item["policy_decision"], "workspace_escape": True}}, "test")


def test_delivery_observer_rejects_unbound_file_and_tamper(smoke, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    artifact = workspace / "evidence.md"
    artifact.write_bytes(b"source-backed")
    scope = hashlib.sha256(b"test").hexdigest()
    receipts = state / "analysis" / scope / "deliveries"
    receipts.mkdir(parents=True)
    value = {"analysis_id": secrets.token_hex(32), "artifact_path": str(artifact),
             "content_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
             "kind": "evidence", "bound_elements": ["rows"],
             "verification": {"delivery_consistency": {"status": "passed"}}}
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    value["record_sha256"] = hashlib.sha256(canonical).hexdigest()
    (receipts / "receipt.json").write_text(json.dumps(value))
    assert smoke.sealed_file_approval_hash(state, "test", workspace, str(artifact)) == value["content_sha256"]
    with pytest.raises(smoke.SmokeFailure, match="^file_delivery_receipt_missing$"):
        smoke.sealed_file_approval_hash(state, "other", workspace, str(artifact))
    artifact.write_bytes(b"tampered")
    with pytest.raises(smoke.SmokeFailure, match="^file_delivery_hash_mismatch$"):
        smoke.sealed_file_approval_hash(state, "test", workspace, str(artifact))
    with pytest.raises(smoke.SmokeFailure, match="^file_delivery_outside_workspace$"):
        smoke.sealed_file_approval_hash(state, "test", workspace, str(tmp_path / "other.md"))


def test_utility_override_only_touches_test_copy_and_excludes_secrets(smoke, tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    original = {"utility_intelligence": {"mode": "dedicated", "model": {
        "provider_id": "old", "model": "old-model", "api_key": "must-not-copy"}, "prompt": "must-not-copy"}}
    blob = json.dumps(original)
    (source / "config.json").write_text(blob)
    (target / "config.json").write_text('{"agents": {}}')
    metadata = smoke.stage_utility_config(source, target, "test-vision", "vision-model")
    assert metadata == {"overridden_in_test_copy": True, "mode": "dedicated",
                        "provider_id": "test-vision", "model": "vision-model"}
    assert (source / "config.json").read_text() == blob
    config = json.loads((target / "config.json").read_text())
    assert "must-not-copy" not in json.dumps(config)
    assert config["utility_intelligence"]["model"] == {"provider_id": "test-vision", "model": "vision-model"}
    with pytest.raises(smoke.SmokeFailure, match="^utility_override_incomplete$"):
        smoke.stage_utility_config(source, target, "test-vision", None)


def test_harness_never_records_backend_logs_or_evidence_transcripts(smoke):
    source = Path(smoke.__file__).read_text(encoding="utf-8")
    assert "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL" in source
    assert "scenario = json.load(sys.stdin)" in source
    assert "evidence_output" not in source
    assert "frame_trace" not in source
    assert "str(error) if isinstance(error, SmokeFailure) else type(error).__name__" in source


def test_projection_observations_are_boolean_only_and_incremental(smoke, tmp_path):
    path = tmp_path / "projection.jsonl"
    path.write_text(
        json.dumps({
            "spec_present": True,
            "spec_name_matches": True,
            "spec_origin_builtin": True,
            "projection_marker_present": False,
            "result_ok": True,
            "reference_shape_present": True,
            "private": "must-not-escape",
        })
        + "\n",
        encoding="utf-8",
    )
    baseline = smoke.projection_observation_snapshot(path)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({
            "spec_present": True,
            "spec_name_matches": True,
            "spec_origin_builtin": True,
            "projection_marker_present": True,
            "result_ok": True,
            "reference_shape_present": True,
            "private": "must-not-escape",
        }) + "\n")

    observations = smoke.projection_observations_since(path, baseline)

    assert observations == [{
        "observe_called": False,
        "spec_present": True,
        "spec_name_matches": True,
        "spec_origin_builtin": True,
        "projection_marker_present": True,
        "result_ok": True,
        "reference_shape_present": True,
        "observe_has_pending_after": False,
        "project_called": False,
        "project_event_eligible": False,
        "project_had_pending_before": False,
        "project_has_pending_after": False,
        "project_added_chart_fence": False,
        "pending_called": False,
        "pending_had_pending_before": False,
        "pending_produced_event": False,
        "pending_event_has_chart_fence": False,
        "run_turn_finalized": False,
        "run_turn_exhausted": False,
        "run_turn_terminal_seen": False,
        "run_turn_cancelled": False,
    }]
    assert "private" not in json.dumps(observations)


def test_public_tool_counts_support_nested_content_data_without_arguments(smoke):
    frames = [(0, {"type": "message", "content": [{"type": "tool_call", "data": {
        "id": "one", "name": "create_analysis_chart", "arguments": "private"}}]}),
              (1, {"type": "message", "content": [{"type": "tool_call", "data": {
                  "id": "one", "name": "create_analysis_chart"}}]}),
              (2, {"type": "message", "content": [{"type": "tool_call", "data": {
                  "id": "two", "name": "private"}}]})]
    assert smoke.tool_counts(frames) == {"create_analysis_chart": 1}


def _ledger_event(
    *, event_id, seq, run_id, item_id, name, kind="chat.tool_call.started",
    event_type="tool.started", status="started", payload=None,
):
    return SimpleNamespace(
        id=event_id,
        seq=seq,
        run_id=run_id,
        chat_id="chat-1",
        session_id="session-1",
        item_id=item_id,
        kind=kind,
        type=event_type,
        status=status,
        payload={"tool_name": name, **(payload or {})},
    )


def test_event_ledger_counts_only_new_allowlisted_tool_starts(smoke):
    before = [_ledger_event(
        event_id="old", seq=10, run_id="run-1", item_id="old-call",
        name="import_data_table",
    )]
    snapshot = smoke.event_ledger_snapshot(before)
    after = before + [
        _ledger_event(event_id="new-1", seq=11, run_id="run-2", item_id="call-1",
                      name="create_analysis_chart", payload={"arguments": "private"}),
        _ledger_event(event_id="new-2", seq=12, run_id="run-2", item_id="call-1",
                      name="create_analysis_chart"),
        _ledger_event(event_id="new-3", seq=13, run_id="run-2", item_id="call-2",
                      name="private", payload={"arguments": "private"}),
        _ledger_event(event_id="new-4", seq=14, run_id="run-2", item_id="call-3",
                      name="create_analysis_chart", event_type="tool.completed"),
    ]

    assert smoke.event_ledger_tool_counts(after, snapshot) == {
        "create_analysis_chart": 1,
    }


def test_event_ledger_ignores_replayed_baseline_call_with_new_event_identity(smoke):
    before = [_ledger_event(
        event_id="old-event", seq=10, run_id="run-1", item_id="stable-call",
        name="import_data_table",
    )]
    snapshot = smoke.event_ledger_snapshot(before)
    replayed = [_ledger_event(
        event_id="replayed-event", seq=11, run_id="run-2", item_id="stable-call",
        name="import_data_table",
    )]

    assert smoke.event_ledger_tool_counts(replayed, snapshot) == {}


def test_event_ledger_call_identity_is_scoped_by_run(smoke):
    snapshot = smoke.event_ledger_snapshot([])
    events = [
        _ledger_event(event_id="one", seq=1, run_id="run-1", item_id="same-call",
                      name="analyze_data_table"),
        _ledger_event(event_id="two", seq=2, run_id="run-2", item_id="same-call",
                      name="analyze_data_table"),
    ]

    assert smoke.event_ledger_tool_counts(events, snapshot) == {
        "analyze_data_table": 2,
    }


def test_ledger_is_authoritative_when_public_stream_omits_tool_frames(smoke):
    frames = [(0, {"object": "response", "status": "completed", "output": []})]
    snapshot = smoke.event_ledger_snapshot([])
    events = [_ledger_event(
        event_id="one", seq=1, run_id="run-1", item_id="call-1",
        name="create_analysis_presentation",
    )]

    assert smoke.tool_counts(frames) == {}
    assert smoke.event_ledger_tool_counts(events, snapshot) == {
        "create_analysis_presentation": 1,
    }


def test_event_ledger_outcomes_are_allowlisted_sanitized_and_new(smoke):
    before = [_ledger_event(
        event_id="old", seq=10, run_id="run-1", item_id="old-call",
        name="create_analysis_presentation", kind="chat.tool_call.finished",
        event_type="tool.completed", status="failed",
        payload={"ok": False, "error": "Presentation filename already exists; choose a new filename"},
    )]
    snapshot = smoke.event_ledger_snapshot(before)
    after = before + [
        _ledger_event(
            event_id="new-1", seq=11, run_id="run-2", item_id="call-1",
            name="create_analysis_presentation", kind="chat.tool_call.finished",
            event_type="tool.completed", status="failed",
            payload={
                "ok": False,
                "error_code": "invalid_presentation_arguments",
                "error": "Presentation title cannot contain unbound numeric facts",
                "output": [{"type": "text", "text": "private"}],
                "arguments": "private",
            },
        ),
        _ledger_event(
            event_id="new-2", seq=12, run_id="run-2", item_id="call-2",
            name="private", kind="chat.tool_call.finished",
            event_type="tool.completed", status="failed",
            payload={"ok": False, "error": "private"},
        ),
    ]

    outcomes = smoke.event_ledger_tool_outcomes(after, snapshot)

    assert outcomes == [{
        "tool": "create_analysis_presentation",
        "status": "failed",
        "error_class": "invalid_presentation_arguments",
        "error_sha256": hashlib.sha256(
            b"Presentation title cannot contain unbound numeric facts",
        ).hexdigest(),
    }]
    assert "private" not in json.dumps(outcomes)


def test_event_ledger_outcomes_hash_unknown_errors_and_deduplicate_calls(smoke):
    snapshot = smoke.event_ledger_snapshot([])
    events = [
        _ledger_event(
            event_id="one", seq=1, run_id="run-1", item_id="call-1",
            name="create_analysis_presentation", kind="chat.tool_call.finished",
            event_type="tool.completed", status="failed",
            payload={"ok": False, "error": "an unexpected private failure"},
        ),
        _ledger_event(
            event_id="two", seq=2, run_id="run-1", item_id="call-1",
            name="create_analysis_presentation", kind="chat.tool_call.finished",
            event_type="tool.completed", status="failed",
            payload={"ok": False, "error": "an unexpected private failure"},
        ),
        _ledger_event(
            event_id="three", seq=3, run_id="run-1", item_id="call-2",
            name="create_analysis_report", kind="chat.tool_call.finished",
            event_type="tool.completed", status="completed",
            payload={"ok": True, "output": [{"type": "text", "text": "private"}]},
        ),
    ]

    outcomes = smoke.event_ledger_tool_outcomes(events, snapshot)

    assert outcomes == [
        {
            "tool": "create_analysis_presentation",
            "status": "failed",
            "error_class": "other",
            "error_sha256": hashlib.sha256(b"an unexpected private failure").hexdigest(),
        },
        {"tool": "create_analysis_report", "status": "completed"},
    ]
    assert "unexpected" not in json.dumps(outcomes)


def test_event_ledger_assistant_projection_shape_exposes_no_text(smoke):
    snapshot = smoke.event_ledger_snapshot([])
    events = [
        SimpleNamespace(
            id="one",
            seq=1,
            run_id="run-1",
            chat_id="chat-1",
            session_id="session-1",
            item_id="message-1",
            kind="chat.message.completed",
            type="message.completed",
            status="completed",
            source="assistant",
            payload={
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "private"}],
                },
                "text": "private",
            },
        ),
        SimpleNamespace(
            id="two",
            seq=2,
            run_id="run-1",
            chat_id="chat-1",
            session_id="session-1",
            item_id="message-2",
            kind="chat.message.completed",
            type="message.completed",
            status="completed",
            source="assistant",
            payload={
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "```analysis-chart\n"
                                + '{"analysis_id":"'
                                + ("a" * 64)
                                + '","type":"bar","x_column":"region",'
                                + '"y_column":"revenue"}\n```'
                            ),
                        }
                    ],
                },
            },
        ),
    ]

    shape = smoke.event_ledger_assistant_projection_shape(events, snapshot)

    assert shape == {
        "assistant_completions": 2,
        "assistant_completions_with_text": 2,
        "assistant_completions_with_chart_fence": 1,
    }
    assert "private" not in json.dumps(shape)


def test_event_ledger_terminal_shape_exposes_only_bounded_error_metadata(smoke):
    before = [SimpleNamespace(
        id="old",
        seq=10,
        run_id="run-1",
        chat_id="chat-1",
        session_id="session-1",
        item_id="turn-1",
        kind="chat.turn.completed",
        type="turn.completed",
        status="completed",
        source="system",
        payload={},
    )]
    snapshot = smoke.event_ledger_snapshot(before)
    events = before + [
        SimpleNamespace(
            id="failed",
            seq=11,
            run_id="run-2",
            chat_id="chat-1",
            session_id="session-1",
            item_id="turn-2",
            kind="chat.turn.failed",
            type="turn.failed",
            status="failed",
            source="system",
            payload={
                "error_code": "missing_visible_answer",
                "error": {"message": "private"},
            },
        ),
        SimpleNamespace(
            id="unknown",
            seq=12,
            run_id="run-3",
            chat_id="chat-1",
            session_id="session-1",
            item_id="turn-3",
            kind="chat.turn.failed",
            type="turn.failed",
            status="failed",
            source="system",
            payload={
                "error_code": "private_runtime_code",
                "error": {"message": "private"},
            },
        ),
    ]

    shape = smoke.event_ledger_terminal_shape(events, snapshot)

    assert shape == [
        {
            "type": "turn.failed",
            "status": "failed",
            "error_code": "missing_visible_answer",
            "error_code_sha256": hashlib.sha256(
                b"missing_visible_answer"
            ).hexdigest(),
        },
        {
            "type": "turn.failed",
            "status": "failed",
            "error_code": "other",
            "error_code_sha256": hashlib.sha256(
                b"private_runtime_code"
            ).hexdigest(),
        },
    ]
    assert "private_runtime_code" not in json.dumps(shape)


def test_missing_verified_delivery_reports_only_allowlisted_kinds(smoke, tmp_path):
    with pytest.raises(
        smoke.SmokeFailure,
        match="^requested_verified_delivery_missing_docx_pptx$",
    ):
        smoke.verify_deliveries(
            tmp_path / "state",
            {"analysis_id": secrets.token_hex(32)},
            {"pptx", "docx"},
            tmp_path / "artifacts",
        )


def test_smoke_trace_declares_private_workspace_ledger_as_count_source(smoke):
    source = Path(smoke.__file__).read_text(encoding="utf-8")

    assert 'trace["analysis_tool_count_source"] = "workspace_event_ledger"' in source
    assert "event_ledger_tool_counts" in source
    assert 'trace["analysis_tool_frames"] = tool_counts(frames)' not in source


def test_failure_metadata_never_retains_provider_text_or_arbitrary_codes(smoke):
    frames = [(0, {"object": "response", "status": "failed", "error": {
        "code": "completion_gate_failed", "message": "private"}}),
              (1, {"object": "response", "status": "failed", "error": {
                  "code": "private", "message": "private"}})]
    metadata = smoke.failure_metadata(frames)
    assert metadata[0]["code"] == "completion_gate_failed"
    assert metadata[1]["code"] == "other"
    assert metadata[1]["code_sha256"] == hashlib.sha256(b"private").hexdigest()
    assert "private" not in json.dumps(metadata)


def test_assistant_surface_shape_exposes_structure_without_text(smoke):
    frames = [
        (0, {"type": "message", "role": "assistant", "content": [
            {"type": "text", "text": "private"},
        ]}),
        (1, {"object": "response", "status": "completed", "output": [
            {"type": "message", "role": "assistant", "content": [
                {"type": "text", "text": "private\n```analysis-chart\n{}\n```"},
            ]},
        ]}),
    ]

    shape = smoke.assistant_surface_shape(frames)

    assert shape == {
        "assistant_messages": 2,
        "assistant_messages_with_text": 2,
        "assistant_messages_with_chart_fence": 1,
        "assistant_content_types": {"text": 2},
    }
    assert "private" not in json.dumps(shape)
