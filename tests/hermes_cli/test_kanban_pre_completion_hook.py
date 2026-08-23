"""Authoritative, typed Kanban completion-veto tests.

The hook is deliberately exercised through ``kanban_db.complete_task`` rather
than a shell command.  A caller that can reach the core transition must not be
able to bypass an enrolled completion policy by choosing a different surface.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.plugins import get_plugin_manager


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_before_kanban_task_complete_veto_blocks_core_api(kanban_home):
    """A typed hook veto must preserve the task's pre-completion state."""
    manager = get_plugin_manager()
    saved = {key: list(value) for key, value in manager._hooks.items()}
    observed = []

    def veto(**payload):
        observed.append(payload)
        return {"allow": False, "reason": "production receipt missing"}

    manager._hooks.setdefault("before_kanban_task_complete", []).append(veto)
    try:
        conn = kb.connect()
        try:
            task_id = kb.create_task(conn, title="delivery-v2 canary", assignee="implementer")
            assert kb.complete_task(
                conn,
                task_id,
                summary="attempt without receipt",
                metadata={"delivery_v2": {"task_class": "PRODUCTION_CHANGE"}},
                actor="implementer",
            ) is False
            assert kb.get_task(conn, task_id).status == "ready"
        finally:
            conn.close()
    finally:
        manager._hooks = saved

    assert len(observed) == 1
    payload = observed[0]
    assert payload["task_id"] == task_id
    assert payload["actor"] == "implementer"
    assert payload["task"].id == task_id
    assert payload["metadata"] == {"delivery_v2": {"task_class": "PRODUCTION_CHANGE"}}


def test_before_kanban_task_complete_veto_blocks_cli(kanban_home, monkeypatch, capsys):
    """The direct CLI reaches the same typed policy boundary as the API."""
    monkeypatch.setenv("HERMES_PROFILE", "  CLI-VERIFIER ")
    manager = get_plugin_manager()
    saved = {key: list(value) for key, value in manager._hooks.items()}
    observed = []

    def veto(**payload):
        observed.append(payload)
        return "independent receipt required"

    manager._hooks.setdefault("before_kanban_task_complete", []).append(veto)
    try:
        conn = kb.connect()
        try:
            task_id = kb.create_task(conn, title="cli canary", assignee="implementer")
        finally:
            conn.close()

        from hermes_cli.kanban import _cmd_complete

        args = argparse.Namespace(
            task_ids=[task_id],
            result=None,
            summary="CLI bypass attempt",
            metadata=json.dumps({"delivery_v2": {"task_class": "PRODUCTION_CHANGE"}}),
        )
        assert _cmd_complete(args) == 1
        assert "cannot complete" in capsys.readouterr().err

        conn = kb.connect()
        try:
            assert kb.get_task(conn, task_id).status == "ready"
        finally:
            conn.close()
    finally:
        manager._hooks = saved

    assert observed[0]["actor"] == "cli-verifier"


def test_malformed_pre_completion_decision_fails_closed(kanban_home):
    """A plugin cannot accidentally allow completion with an ambiguous reply."""
    manager = get_plugin_manager()
    saved = {key: list(value) for key, value in manager._hooks.items()}
    manager._hooks.setdefault("before_kanban_task_complete", []).append(
        lambda **_: {"allow": "yes"}
    )
    try:
        conn = kb.connect()
        try:
            task_id = kb.create_task(conn, title="malformed policy", assignee="implementer")
            assert kb.complete_task(conn, task_id, summary="attempt") is False
            assert kb.get_task(conn, task_id).status == "ready"
            events = conn.execute(
                "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id",
                (task_id,),
            ).fetchall()
        finally:
            conn.close()
    finally:
        manager._hooks = saved

    assert events[-1]["kind"] == "completion_blocked_policy"
    assert "malformed" in json.loads(events[-1]["payload"])["reason"]


def _install_delivery_v2_policy():
    from plugins.delivery_v2 import before_kanban_task_complete

    manager = get_plugin_manager()
    saved = {key: list(value) for key, value in manager._hooks.items()}
    manager._hooks.setdefault("before_kanban_task_complete", []).append(
        before_kanban_task_complete
    )
    return manager, saved


def _production_card_body():
    return json.dumps({"delivery_v2": {
        "classification": "PRODUCTION_CHANGE",
        "role": "PRODUCTION_VERIFIER",
        "state": "PRODUCTION_VERIFY",
        "implementer": "implementer",
    }})


def _valid_production_receipt():
    return {"production_receipt": {
        "MERGED_SHA": "a" * 40,
        "DEPLOYED_SHA": "a" * 40,
        "PRODUCTION_URL": "https://delivery.example.invalid",
        "DEPLOYMENT_STATUS": "SUCCESS",
        "EXACT_SHA_MATCH": "PASS",
        "PRODUCTION_ACCEPTANCE_PROBE": "PASS",
        "USER_VISIBLE_DELTA_EVIDENCE": "sandbox probe",
        "ROLLBACK_OR_REVERT_PATH": "git revert <sha>",
        "ONE_BRANCH_ONE_WRITER": "PASS",
        "PRODUCT_PLATFORM_PR_SEPARATION": "PASS",
        "HEAD_FROZEN": "PASS",
        "VERIFIER": " verifier ",
    }, "delivery_controls": {
        "writer_ids": ["implementer"],
        "product_files": ["src/product.py"],
        "platform_files": [],
        "preview_head_sha": "b" * 40,
        "frozen_head_sha": "b" * 40,
        "reviewed_head_sha": "b" * 40,
        "post_freeze_correction_count": 0,
        "commit_count": 1,
        "changed_files": 1,
        "net_lines": 20,
        "minutes_without_preview": 5,
    }}


def _valid_structured_production_evidence(conn, task_id, verifier_run_id):
    exact_sha = "a" * 40
    kb._set_worker_pid(conn, task_id, os.getpid())
    implementer_run_id = conn.execute(
        "SELECT id FROM task_runs WHERE task_id = ? AND profile = 'implementer' "
        "AND ended_at IS NOT NULL ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()["id"]
    refs = []
    for kind in ("merge", "deployment", "acceptance", "user_visible_delta", "rollback"):
        document = {
            "kind": kind,
            "scope": "PRODUCTION",
            "exact_sha": exact_sha,
            "verifier_run_id": verifier_run_id,
        }
        if kind == "deployment":
            document.update({
                "deployment_status": "SUCCESS",
                "production_url": "https://delivery.example.com",
            })
        elif kind == "acceptance":
            document.update({
                "acceptance_status": "PASS",
                "production_url": "https://delivery.example.com",
            })
        elif kind == "user_visible_delta":
            document["evidence"] = "independently observed production delta"
        elif kind == "rollback":
            document["rollback_target_sha"] = "c" * 40
        data = json.dumps(document, sort_keys=True).encode()
        attachment_id = kb.store_attachment_bytes(
            conn,
            task_id,
            f"{kind}.json",
            data,
            content_type="application/json",
            uploaded_by="verifier",
        )
        refs.append({
            "kind": kind,
            "scope": "PRODUCTION",
            "ref": f"kanban-attachment:{attachment_id}",
            "attachment_id": attachment_id,
            "sha256": hashlib.sha256(data).hexdigest(),
            "exact_sha": exact_sha,
        })
    metadata = _valid_production_receipt()
    metadata.pop("production_receipt")
    metadata["production_evidence"] = {
        "scope": "PRODUCTION",
        "merged_sha": exact_sha,
        "deployed_sha": exact_sha,
        "production_url": "https://delivery.example.com",
        "deployment_status": "SUCCESS",
        "acceptance_status": "PASS",
        "rollback_target_sha": "c" * 40,
        "ONE_BRANCH_ONE_WRITER": "PASS",
        "PRODUCT_PLATFORM_PR_SEPARATION": "PASS",
        "HEAD_FROZEN": "PASS",
        "verifier_run_id": verifier_run_id,
        "verifier_session_id": "session-verifier-1",
        "implementer_run_id": implementer_run_id,
        "refs": refs,
    }
    return metadata


def test_delivery_v2_plugin_blocks_then_allows_independent_receipt(
    kanban_home, monkeypatch,
):
    """The policy uses the typed kernel hook, not terminal text parsing."""
    manager, saved = _install_delivery_v2_policy()
    try:
        conn = kb.connect()
        try:
            task_id = kb.create_task(
                conn, title="production canary", body=_production_card_body(),
                assignee="implementer",
            )
            assert kb.set_task_workflow_step(
                conn, task_id, workflow_template_id="anveros-delivery-v2",
                current_step_key="PRODUCTION_VERIFY",
            )
            assert kb.claim_task(conn, task_id) is not None
            assert kb.request_review(
                conn, task_id, reviewer="verifier",
                expected_run_id=kb.get_task(conn, task_id).current_run_id,
            ) is True
            assert kb.complete_task(conn, task_id, actor=" verifier ", summary="no receipt") is False
            assert kb.get_task(conn, task_id).status == "review"
            assert kb.claim_review_task(conn, task_id, claimer="verifier") is not None
            verifier_run_id = kb.get_task(conn, task_id).current_run_id
            monkeypatch.setenv("HERMES_PROFILE", "verifier")
            monkeypatch.setenv("HERMES_SESSION_ID", "session-verifier-1")
            monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
            monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(verifier_run_id))
            assert kb.complete_task(
                conn, task_id, actor=" VERIFIER ", summary="verified",
                metadata=_valid_structured_production_evidence(
                    conn, task_id, verifier_run_id,
                ),
                expected_run_id=verifier_run_id,
            ) is True
            assert kb.get_task(conn, task_id).status == "done"
            assert kb.get_task(conn, task_id).current_step_key == "DONE"
        finally:
            conn.close()
    finally:
        manager._hooks = saved


def test_delivery_v2_plugin_keeps_nonproduction_completion_normal(kanban_home):
    manager, saved = _install_delivery_v2_policy()
    try:
        conn = kb.connect()
        try:
            body = json.dumps({"delivery_v2": {
                "classification": "NON_PRODUCTION_DELIVERABLE",
                "role": "RESEARCHER",
            }})
            task_id = kb.create_task(conn, title="read-only receipt", body=body)
            assert kb.complete_task(conn, task_id, actor="researcher", summary="inventory") is True
            assert kb.get_task(conn, task_id).status == "done"
        finally:
            conn.close()
    finally:
        manager._hooks = saved


def test_delivery_v2_strict_cutover_requires_new_task_classification(
    kanban_home, monkeypatch,
):
    import plugins.delivery_v2 as policy

    manager, saved = _install_delivery_v2_policy()
    monkeypatch.setattr(policy, "_STRICT_AFTER_EPOCH", 1)
    try:
        conn = kb.connect()
        try:
            task_id = kb.create_task(conn, title="unclassified post-cutover card")
            assert kb.complete_task(
                conn, task_id, actor="researcher", summary="must classify",
            ) is False
            assert kb.get_task(conn, task_id).status == "ready"
            event = conn.execute(
                "SELECT payload FROM task_events WHERE task_id = ? "
                "AND kind = 'completion_blocked_policy' ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        finally:
            conn.close()
    finally:
        manager._hooks = saved

    assert "TASK_CLASSIFICATION_REQUIRED" in json.loads(event["payload"])["reason"]


def test_delivery_v2_plugin_rejects_negative_delivery_controls(
    kanban_home, monkeypatch,
):
    """#701-shaped evidence cannot be promoted by activity or a preview."""
    manager, saved = _install_delivery_v2_policy()
    try:
        conn = kb.connect()
        try:
            task_id = kb.create_task(
                conn, title="negative delivery controls", body=_production_card_body(),
                assignee="implementer",
            )
            assert kb.set_task_workflow_step(
                conn, task_id, workflow_template_id="anveros-delivery-v2",
                current_step_key="PRODUCTION_VERIFY",
            )
            assert kb.claim_task(conn, task_id, claimer="implementer") is not None
            assert kb.request_review(
                conn, task_id, reviewer="verifier",
                expected_run_id=kb.get_task(conn, task_id).current_run_id,
            ) is True
            assert kb.claim_review_task(conn, task_id, claimer="verifier") is not None
            verifier_run_id = kb.get_task(conn, task_id).current_run_id
            monkeypatch.setenv("HERMES_PROFILE", "verifier")
            monkeypatch.setenv("HERMES_SESSION_ID", "session-verifier-1")
            monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
            monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(verifier_run_id))
            metadata = _valid_structured_production_evidence(
                conn, task_id, verifier_run_id,
            )
            metadata["delivery_controls"].update({
                "writer_ids": ["implementer", "second-writer"],
                "platform_files": [".github/workflows/ci.yml"],
                "reviewed_head_sha": "c" * 40,
                "commit_count": 50,
                "changed_files": 29,
                "net_lines": 1667,
                "minutes_without_preview": 120,
            })
            assert kb.complete_task(
                conn, task_id, actor="verifier", summary="negative canary",
                metadata=metadata,
                expected_run_id=verifier_run_id,
            ) is False
            event = conn.execute(
                "SELECT payload FROM task_events WHERE task_id = ? "
                "AND kind = 'completion_blocked_policy' ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        finally:
            conn.close()
    finally:
        manager._hooks = saved

    reason = json.loads(event["payload"])["reason"]
    assert "ONE_BRANCH_ONE_WRITER_FAILED" in reason
    assert "PRODUCT_PLATFORM_PR_MIXED" in reason
    assert "HEAD_MOVED_AFTER_PREVIEW" in reason
    assert "STOP_SPLIT_COMMITS" in reason


def test_delivery_v2_plugin_allows_verifier_owned_review_run(kanban_home, monkeypatch):
    """Gateway claims the reviewer before it invokes the completion boundary."""
    manager, saved = _install_delivery_v2_policy()
    try:
        conn = kb.connect()
        try:
            task_id = kb.create_task(
                conn, title="production verifier run", body=_production_card_body(),
                assignee="implementer",
            )
            assert kb.set_task_workflow_step(
                conn, task_id, workflow_template_id="anveros-delivery-v2",
                current_step_key="PRODUCTION_VERIFY",
            )
            assert kb.claim_task(conn, task_id, claimer="implementer") is not None
            assert kb.request_review(
                conn, task_id, reviewer="verifier",
                expected_run_id=kb.get_task(conn, task_id).current_run_id,
            ) is True
            assert kb.claim_review_task(conn, task_id, claimer="verifier") is not None
            verifier_run_id = kb.get_task(conn, task_id).current_run_id
            monkeypatch.setenv("HERMES_PROFILE", "verifier")
            monkeypatch.setenv("HERMES_SESSION_ID", "session-verifier-1")
            monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
            monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(verifier_run_id))
            assert kb.complete_task(
                conn, task_id, actor="verifier", summary="verified run",
                metadata=_valid_structured_production_evidence(
                    conn, task_id, verifier_run_id,
                ),
                expected_run_id=verifier_run_id,
            ) is True
            assert kb.get_task(conn, task_id).status == "done"
            assert kb.get_task(conn, task_id).current_step_key == "DONE"
        finally:
            conn.close()
    finally:
        manager._hooks = saved


def test_workflow_step_is_durable_and_compare_and_set(kanban_home):
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="durable workflow step")
        assert kb.set_task_workflow_step(
            conn,
            task_id,
            workflow_template_id="anveros-delivery-v2",
            current_step_key="READY",
            reason="card admitted",
        ) is True
        task = kb.get_task(conn, task_id)
        assert task.workflow_template_id == "anveros-delivery-v2"
        assert task.current_step_key == "READY"
        assert kb.set_task_workflow_step(
            conn,
            task_id,
            workflow_template_id="anveros-delivery-v2",
            current_step_key="IMPLEMENTING",
            expected_current_step_key="TRIAGE",
        ) is False
        assert kb.set_task_workflow_step(
            conn,
            task_id,
            workflow_template_id="anveros-delivery-v2",
            current_step_key="IMPLEMENTING",
            expected_current_step_key="READY",
        ) is True
        event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = 'workflow_step_changed' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()

    assert json.loads(event["payload"])["current_step_key"] == "IMPLEMENTING"


def _phase_b_card_body():
    return json.dumps({"delivery_v2": {
        "classification": "NON_PRODUCTION_DELIVERABLE",
        "role": "IMPLEMENTER",
        "state": "READY",
        "implementer": "coder",
        "role_profiles": {
            "IMPLEMENTER": "coder",
            "PRODUCT_VERIFIER": "verifier",
            "PLATFORM_FIXER": "ops",
            "RELEASE_OWNER": "orchestrator",
            "PRODUCTION_VERIFIER": "verifier",
        },
        "fallback_profiles": {"coder": "ops"},
    }})


def test_phase_b_transition_persists_steps_and_hands_off_review(kanban_home, monkeypatch):
    from plugins.delivery_v2 import transition_delivery_state

    monkeypatch.setenv("HERMES_PROFILE", "coder")
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn, title="phase b state canary", body=_phase_b_card_body(), assignee="coder",
        )
        assert kb.claim_task(conn, task_id, claimer="coder") is not None
        run_id = kb.get_task(conn, task_id).current_run_id
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))

    assert "READY -> IMPLEMENTING" in transition_delivery_state({
        "task_id": task_id, "next_state": "IMPLEMENTING",
    }, task_id=task_id)
    assert "IMPLEMENTING -> PREVIEW_READY" in transition_delivery_state({
        "task_id": task_id, "next_state": "PREVIEW_READY",
    }, task_id=task_id)
    assert "PREVIEW_READY -> PRODUCT_REVIEW" in transition_delivery_state({
        "task_id": task_id, "next_state": "PRODUCT_REVIEW",
    }, task_id=task_id)

    conn = kb.connect()
    try:
        task = kb.get_task(conn, task_id)
        assert task.workflow_template_id == "anveros-delivery-v2"
        assert task.current_step_key == "PRODUCT_REVIEW"
        assert task.assignee == "verifier"
        assert task.status == "review"
    finally:
        conn.close()


def test_phase_b_dispatch_reroutes_only_an_explicit_sandbox_card(kanban_home):
    from plugins.delivery_v2 import on_kanban_dispatch_tick

    body = json.loads(_phase_b_card_body())
    body["delivery_v2"].update({
        "canary": "SANDBOX_ONLY", "auto_reroute_after_seconds": 1,
    })
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn, title="phase b reroute canary", body=json.dumps(body), assignee="coder",
        )
        conn.execute("UPDATE tasks SET created_at = 0 WHERE id = ?", (task_id,))
        conn.execute(
            "UPDATE task_events SET created_at = 0 WHERE task_id = ?",
            (task_id,),
        )
    finally:
        conn.close()

    on_kanban_dispatch_tick(board="default")
    conn = kb.connect()
    try:
        assert kb.get_task(conn, task_id).assignee == "ops"
    finally:
        conn.close()


def test_phase_b_verifier_block_reassigns_same_card_to_platform_fixer(kanban_home):
    from plugins.delivery_v2 import on_kanban_task_blocked

    body = json.loads(_phase_b_card_body())
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn, title="verifier block escalation", body=json.dumps(body),
            assignee="verifier",
        )
        assert kb.set_task_workflow_step(
            conn, task_id, workflow_template_id="anveros-delivery-v2",
            current_step_key="PRODUCT_REVIEW",
        )
        assert kb.block_task(conn, task_id, reason="preview provenance mismatch", kind="capability")
        task_count = conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
    finally:
        conn.close()

    on_kanban_task_blocked(
        task_id=task_id, board="default", assignee="verifier",
        reason="preview provenance mismatch",
    )
    conn = kb.connect()
    try:
        task = kb.get_task(conn, task_id)
        assert task.status == "blocked"
        assert task.assignee == "ops"
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == task_count
    finally:
        conn.close()


def test_phase_b_transient_verifier_block_resumes_same_card_with_fixer(kanban_home):
    from plugins.delivery_v2 import on_kanban_task_blocked

    body = json.loads(_phase_b_card_body())
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn, title="transient verifier fallback", body=json.dumps(body),
            assignee="verifier",
        )
        assert kb.set_task_workflow_step(
            conn, task_id, workflow_template_id="anveros-delivery-v2",
            current_step_key="PRODUCT_REVIEW",
        )
        assert kb.block_task(conn, task_id, reason="provider unavailable", kind="transient")
        task_count = conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
    finally:
        conn.close()

    on_kanban_task_blocked(
        task_id=task_id, board="default", assignee="verifier",
        reason="provider unavailable",
    )
    conn = kb.connect()
    try:
        task = kb.get_task(conn, task_id)
        assert task.status == "ready"
        assert task.assignee == "ops"
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == task_count
        kinds = [
            row["kind"] for row in conn.execute(
                "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id",
                (task_id,),
            ).fetchall()
        ]
        assert kinds[-3:] == ["blocked", "assigned", "unblocked"]
    finally:
        conn.close()


def test_phase_b_done_has_no_post_commit_observer():
    import plugins.delivery_v2 as policy

    assert not hasattr(policy, "on_kanban_task_completed")


def test_delivery_v2_transition_rejects_model_actor_without_active_run(
    kanban_home, monkeypatch,
):
    from plugins.delivery_v2 import transition_delivery_state

    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn, title="spoofed transition actor", body=_phase_b_card_body(),
            assignee="coder",
        )
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_PROFILE", "coder")
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)

    result = transition_delivery_state({
        "task_id": task_id, "actor": "coder", "next_state": "IMPLEMENTING",
    }, task_id=task_id)
    assert "trusted active run provenance" in result


def test_delivery_v2_composite_transition_rolls_back_on_handoff_failure(
    kanban_home, monkeypatch,
):
    from plugins.delivery_v2 import transition_delivery_state

    monkeypatch.setenv("HERMES_PROFILE", "coder")
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn, title="atomic review handoff", body=_phase_b_card_body(),
            assignee="coder",
        )
        assert kb.claim_task(conn, task_id, claimer="coder") is not None
        run_id = kb.get_task(conn, task_id).current_run_id
        conn.execute(
            "CREATE TRIGGER fail_review_event BEFORE INSERT ON task_events "
            "WHEN NEW.kind = 'review_requested' BEGIN "
            "SELECT RAISE(ABORT, 'injected review handoff failure'); END"
        )
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))

    assert "READY -> IMPLEMENTING" in transition_delivery_state({
        "task_id": task_id, "next_state": "IMPLEMENTING",
    }, task_id=task_id)
    assert "IMPLEMENTING -> PREVIEW_READY" in transition_delivery_state({
        "task_id": task_id, "next_state": "PREVIEW_READY",
    }, task_id=task_id)
    result = transition_delivery_state({
        "task_id": task_id, "next_state": "PRODUCT_REVIEW",
    }, task_id=task_id)
    assert result.startswith("Error:")
    conn = kb.connect()
    try:
        task = kb.get_task(conn, task_id)
        assert task.current_step_key == "PREVIEW_READY"
        assert task.status == "running"
        assert task.assignee == "coder"
    finally:
        conn.close()


def test_delivery_v2_reassign_handoff_rolls_back_as_one_transaction(
    kanban_home, monkeypatch,
):
    from plugins.delivery_v2 import transition_delivery_state

    body = json.loads(_phase_b_card_body())
    body["delivery_v2"]["state"] = "HEAD_FROZEN"
    monkeypatch.setenv("HERMES_PROFILE", "verifier")
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn, title="atomic release handoff", body=json.dumps(body),
            assignee="verifier",
        )
        assert kb.set_task_workflow_step(
            conn, task_id, workflow_template_id="anveros-delivery-v2",
            current_step_key="HEAD_FROZEN",
        )
        assert kb.claim_task(conn, task_id, claimer="verifier") is not None
        run_id = kb.get_task(conn, task_id).current_run_id
        conn.execute(
            "CREATE TRIGGER fail_assign_event BEFORE INSERT ON task_events "
            "WHEN NEW.kind = 'assigned' BEGIN "
            "SELECT RAISE(ABORT, 'injected reassign handoff failure'); END"
        )
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))

    result = transition_delivery_state({
        "task_id": task_id, "next_state": "MERGE_QUEUED",
    }, task_id=task_id)
    assert result.startswith("Error:")
    conn = kb.connect()
    try:
        task = kb.get_task(conn, task_id)
        assert task.current_step_key == "HEAD_FROZEN"
        assert task.status == "running"
        assert task.assignee == "verifier"
        assert task.current_run_id == run_id
    finally:
        conn.close()


def test_initial_null_workflow_step_compare_and_set_is_not_unconditional(kanban_home):
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="initial null cas")
        assert kb.set_task_workflow_step(
            conn, task_id, workflow_template_id="anveros-delivery-v2",
            current_step_key="IMPLEMENTING", expected_current_step_key=None,
        ) is True
        assert kb.set_task_workflow_step(
            conn, task_id, workflow_template_id="anveros-delivery-v2",
            current_step_key="PREVIEW_READY", expected_current_step_key=None,
        ) is False
    finally:
        conn.close()


def test_two_concurrent_initial_null_transitions_have_one_winner(kanban_home):
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="concurrent initial null cas")
    finally:
        conn.close()

    def attempt(step):
        local = kb.connect()
        try:
            return kb.set_task_workflow_step(
                local,
                task_id,
                workflow_template_id="anveros-delivery-v2",
                current_step_key=step,
                expected_current_step_key=None,
            )
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ("IMPLEMENTING", "ROLLBACK")))
    assert sorted(results) == [False, True]


def test_delivery_v2_transition_rejects_stale_worker_run(kanban_home, monkeypatch):
    from plugins.delivery_v2 import transition_delivery_state

    monkeypatch.setenv("HERMES_PROFILE", "coder")
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn, title="stale run transition", body=_phase_b_card_body(),
            assignee="coder",
        )
        first = kb.claim_task(conn, task_id, claimer="coder")
        stale_run_id = first.current_run_id
        assert kb.reclaim_task(conn, task_id, reason="test stale run")
        second = kb.claim_task(conn, task_id, claimer="coder")
        assert second.current_run_id != stale_run_id
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(stale_run_id))

    result = transition_delivery_state({
        "task_id": task_id, "next_state": "IMPLEMENTING",
    }, task_id=task_id)
    assert "trusted active run provenance" in result
    conn = kb.connect()
    try:
        assert kb.get_task(conn, task_id).current_step_key is None
    finally:
        conn.close()


def test_completion_cas_rejects_policy_to_done_state_race(kanban_home, monkeypatch):
    manager = get_plugin_manager()
    saved = {key: list(value) for key, value in manager._hooks.items()}
    manager._hooks.setdefault("before_kanban_task_complete", []).append(
        lambda **_: {"allow": True, "workflow_terminal_step": "DONE"}
    )
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="completion state race", assignee="verifier")
        assert kb.set_task_workflow_step(
            conn, task_id, workflow_template_id="anveros-delivery-v2",
            current_step_key="PRODUCTION_VERIFY",
        )
        claimed = kb.claim_task(conn, task_id, claimer="verifier")
        run_id = claimed.current_run_id
        original = kb._before_kanban_task_complete

        def race_state(local_conn, local_task_id, **kwargs):
            decision = original(local_conn, local_task_id, **kwargs)
            local_conn.execute(
                "UPDATE tasks SET current_step_key = 'ROLLBACK' WHERE id = ?",
                (local_task_id,),
            )
            return decision

        monkeypatch.setattr(kb, "_before_kanban_task_complete", race_state)
        assert kb.complete_task(
            conn, task_id, expected_run_id=run_id, actor="verifier",
            summary="racing completion",
        ) is False
        task = kb.get_task(conn, task_id)
        assert task.status == "running"
        assert task.current_step_key == "ROLLBACK"
    finally:
        conn.close()
        manager._hooks = saved


def test_sandbox_string_receipt_is_not_production_evidence(kanban_home):
    manager, saved = _install_delivery_v2_policy()
    try:
        conn = kb.connect()
        try:
            task_id = kb.create_task(
                conn, title="synthetic production receipt", body=_production_card_body(),
                assignee="implementer",
            )
            assert kb.claim_task(conn, task_id, claimer="implementer") is not None
            assert kb.request_review(
                conn, task_id, reviewer="verifier",
                expected_run_id=kb.get_task(conn, task_id).current_run_id,
            )
            assert kb.claim_review_task(conn, task_id, claimer="verifier") is not None
            assert kb.complete_task(
                conn, task_id, actor="verifier", summary="sandbox strings",
                metadata=_valid_production_receipt(),
                expected_run_id=kb.get_task(conn, task_id).current_run_id,
            ) is False
        finally:
            conn.close()
    finally:
        manager._hooks = saved


def test_structured_sandbox_refs_cannot_satisfy_production_gate(
    kanban_home, monkeypatch,
):
    manager, saved = _install_delivery_v2_policy()
    try:
        conn = kb.connect()
        try:
            task_id = kb.create_task(
                conn, title="structured sandbox evidence", body=_production_card_body(),
                assignee="implementer",
            )
            assert kb.set_task_workflow_step(
                conn, task_id, workflow_template_id="anveros-delivery-v2",
                current_step_key="PRODUCTION_VERIFY",
            )
            assert kb.claim_task(conn, task_id, claimer="implementer") is not None
            assert kb.request_review(
                conn, task_id, reviewer="verifier",
                expected_run_id=kb.get_task(conn, task_id).current_run_id,
            )
            assert kb.claim_review_task(conn, task_id, claimer="verifier") is not None
            verifier_run_id = kb.get_task(conn, task_id).current_run_id
            monkeypatch.setenv("HERMES_PROFILE", "verifier")
            monkeypatch.setenv("HERMES_SESSION_ID", "session-verifier-1")
            monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
            monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(verifier_run_id))
            metadata = _valid_structured_production_evidence(
                conn, task_id, verifier_run_id,
            )
            metadata["production_evidence"]["scope"] = "SANDBOX_ONLY"
            for ref in metadata["production_evidence"]["refs"]:
                ref["scope"] = "SANDBOX_ONLY"
            assert kb.complete_task(
                conn, task_id, actor="verifier", summary="sandbox evidence",
                metadata=metadata, expected_run_id=verifier_run_id,
            ) is False
        finally:
            conn.close()
    finally:
        manager._hooks = saved


def test_production_verifier_must_match_active_run_provenance(kanban_home):
    manager, saved = _install_delivery_v2_policy()
    try:
        conn = kb.connect()
        try:
            task_id = kb.create_task(
                conn, title="spoofed verifier strings", body=_production_card_body(),
                assignee="implementer",
            )
            assert kb.set_task_workflow_step(
                conn, task_id, workflow_template_id="anveros-delivery-v2",
                current_step_key="PRODUCTION_VERIFY",
            )
            claimed = kb.claim_task(conn, task_id, claimer="implementer")
            assert kb.complete_task(
                conn, task_id, actor="verifier", summary="spoofed verifier",
                metadata=_valid_production_receipt(),
                expected_run_id=claimed.current_run_id,
            ) is False
            assert kb.get_task(conn, task_id).status == "running"
        finally:
            conn.close()
    finally:
        manager._hooks = saved


def test_production_completion_rejects_expired_claim_provenance(
    kanban_home, monkeypatch,
):
    manager, saved = _install_delivery_v2_policy()
    try:
        conn = kb.connect()
        try:
            task_id = kb.create_task(
                conn, title="expired verifier claim", body=_production_card_body(),
                assignee="implementer",
            )
            assert kb.set_task_workflow_step(
                conn, task_id, workflow_template_id="anveros-delivery-v2",
                current_step_key="PRODUCTION_VERIFY",
            )
            assert kb.claim_task(conn, task_id, claimer="implementer") is not None
            assert kb.request_review(
                conn, task_id, reviewer="verifier",
                expected_run_id=kb.get_task(conn, task_id).current_run_id,
            )
            assert kb.claim_review_task(conn, task_id, claimer="verifier") is not None
            verifier_run_id = kb.get_task(conn, task_id).current_run_id
            monkeypatch.setenv("HERMES_PROFILE", "verifier")
            monkeypatch.setenv("HERMES_SESSION_ID", "session-verifier-1")
            monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
            monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(verifier_run_id))
            conn.execute(
                "UPDATE tasks SET claim_expires = 1 WHERE id = ?", (task_id,),
            )
            conn.execute(
                "UPDATE task_runs SET claim_expires = 1 WHERE id = ?",
                (verifier_run_id,),
            )
            assert kb.complete_task(
                conn, task_id, actor="verifier", summary="expired claim",
                metadata=_valid_structured_production_evidence(
                    conn, task_id, verifier_run_id,
                ),
                expected_run_id=verifier_run_id,
            ) is False
            assert kb.get_task(conn, task_id).status == "running"
        finally:
            conn.close()
    finally:
        manager._hooks = saved


def test_unclaimed_reroute_uses_latest_assignment_time_not_created_at(kanban_home):
    from plugins.delivery_v2 import on_kanban_dispatch_tick

    body = json.loads(_phase_b_card_body())
    body["delivery_v2"].update({
        "canary": "SANDBOX_ONLY", "auto_reroute_after_seconds": 60,
    })
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn, title="recent assignment must wait", body=json.dumps(body),
            assignee="coder",
        )
        conn.execute("UPDATE tasks SET created_at = 0 WHERE id = ?", (task_id,))
        assert kb.assign_task(conn, task_id, "coder")
    finally:
        conn.close()

    on_kanban_dispatch_tick(board="default")
    conn = kb.connect()
    try:
        assert kb.get_task(conn, task_id).assignee == "coder"
    finally:
        conn.close()


def test_unclaimed_reroute_has_durable_one_shot_marker(kanban_home):
    from plugins.delivery_v2 import on_kanban_dispatch_tick

    body = json.loads(_phase_b_card_body())
    body["delivery_v2"].update({
        "canary": "SANDBOX_ONLY",
        "auto_reroute_after_seconds": 1,
        "fallback_profiles": {"coder": "ops", "ops": "verifier"},
    })
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn, title="one shot reroute", body=json.dumps(body), assignee="coder",
        )
        conn.execute("UPDATE tasks SET created_at = 0 WHERE id = ?", (task_id,))
        conn.execute(
            "UPDATE task_events SET created_at = 0 WHERE task_id = ?",
            (task_id,),
        )
    finally:
        conn.close()

    on_kanban_dispatch_tick(board="default")
    on_kanban_dispatch_tick(board="default")
    conn = kb.connect()
    try:
        task = kb.get_task(conn, task_id)
        assert task.assignee == "ops"
        marker_count = conn.execute(
            "SELECT count(*) FROM task_events WHERE task_id = ? "
            "AND kind = 'delivery_v2_rerouted'",
            (task_id,),
        ).fetchone()[0]
        assert marker_count == 1
    finally:
        conn.close()
