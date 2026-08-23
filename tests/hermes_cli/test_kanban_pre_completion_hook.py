"""Authoritative, typed Kanban completion-veto tests.

The hook is deliberately exercised through ``kanban_db.complete_task`` rather
than a shell command.  A caller that can reach the core transition must not be
able to bypass an enrolled completion policy by choosing a different surface.
"""

from __future__ import annotations

import argparse
import json
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
        "VERIFIER": " verifier ",
    }}


def test_delivery_v2_plugin_blocks_then_allows_independent_receipt(kanban_home):
    """The policy uses the typed kernel hook, not terminal text parsing."""
    manager, saved = _install_delivery_v2_policy()
    try:
        conn = kb.connect()
        try:
            task_id = kb.create_task(
                conn, title="production canary", body=_production_card_body(),
                assignee="implementer",
            )
            assert kb.claim_task(conn, task_id) is not None
            assert kb.request_review(
                conn, task_id, reviewer="verifier",
                expected_run_id=kb.get_task(conn, task_id).current_run_id,
            ) is True
            assert kb.complete_task(conn, task_id, actor=" verifier ", summary="no receipt") is False
            assert kb.get_task(conn, task_id).status == "review"
            assert kb.complete_task(
                conn, task_id, actor=" VERIFIER ", summary="verified",
                metadata=_valid_production_receipt(),
            ) is True
            assert kb.get_task(conn, task_id).status == "done"
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


def test_delivery_v2_plugin_allows_verifier_owned_review_run(kanban_home):
    """Gateway claims the reviewer before it invokes the completion boundary."""
    manager, saved = _install_delivery_v2_policy()
    try:
        conn = kb.connect()
        try:
            task_id = kb.create_task(
                conn, title="production verifier run", body=_production_card_body(),
                assignee="implementer",
            )
            assert kb.claim_task(conn, task_id, claimer="implementer") is not None
            assert kb.request_review(
                conn, task_id, reviewer="verifier",
                expected_run_id=kb.get_task(conn, task_id).current_run_id,
            ) is True
            assert kb.claim_review_task(conn, task_id, claimer="verifier") is not None
            assert kb.complete_task(
                conn, task_id, actor="verifier", summary="verified run",
                metadata=_valid_production_receipt(),
            ) is True
            assert kb.get_task(conn, task_id).status == "done"
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


def test_phase_b_transition_persists_steps_and_hands_off_review(kanban_home):
    from plugins.delivery_v2 import transition_delivery_state

    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn, title="phase b state canary", body=_phase_b_card_body(), assignee="coder",
        )
    finally:
        conn.close()

    assert "READY -> IMPLEMENTING" in transition_delivery_state({
        "task_id": task_id, "actor": "coder", "next_state": "IMPLEMENTING",
    })
    assert "IMPLEMENTING -> PREVIEW_READY" in transition_delivery_state({
        "task_id": task_id, "actor": "coder", "next_state": "PREVIEW_READY",
    })
    assert "PREVIEW_READY -> PRODUCT_REVIEW" in transition_delivery_state({
        "task_id": task_id, "actor": "coder", "next_state": "PRODUCT_REVIEW",
    })

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
    finally:
        conn.close()

    on_kanban_dispatch_tick(board="default")
    conn = kb.connect()
    try:
        assert kb.get_task(conn, task_id).assignee == "ops"
    finally:
        conn.close()
