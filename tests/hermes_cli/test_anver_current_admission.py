from __future__ import annotations

from pathlib import Path
import json
import subprocess

from hermes_cli import kanban_db as kb
from hermes_cli import anver_current_admission as admission
from hermes_cli import config as hermes_config


def _authority_repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "authority"
    source = root / "control_plane" / "founder_intent.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def validate_current_contract(value):\n"
        "    if value.get('schema_version') != 'anveros.current-founder-contract.v1':\n"
        "        raise ValueError('bad schema')\n"
        "    if value.get('state') != 'CURRENT':\n"
        "        raise ValueError('not current')\n"
        "    return value\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(root), "add", "control_plane/founder_intent.py"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "authority"], check=True)
    sha = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    return root, sha


def _bound_body(contract: dict, *, kind: str = "worker") -> str:
    payload = {
        "schema_version": "anveros.hermes-task-payload.v1",
        "execution_authority": "hermes_kanban",
        "kind": kind,
        "current_contract": contract,
        "contract_digest": contract["contract_digest"],
        "contract_version": contract["version"],
        "work_identity": contract["work_identity"],
        "writer_execution_allowed": contract["writer_execution_allowed"],
    }
    return "bounded task\n\nANVER_HERMES_TASK_PAYLOAD=" + json.dumps(payload)


def _contract(version: int, digest: str) -> dict:
    return {
        "schema_version": "anveros.current-founder-contract.v1",
        "state": "CURRENT",
        "version": version,
        "work_identity": "anver-work:cutover",
        "contract_digest": digest,
        "writer_execution_allowed": True,
    }


def _unbound_stale_task(conn) -> str:
    task_id = kb.create_task(
        conn,
        title="PR #213 remote exact-head re-review",
        body="Re-open old exact head 242eee without a CURRENT contract binding.",
        assignee="auditor",
        workspace_kind="scratch",
        initial_status="blocked",
    )
    assert kb.unblock_task(conn, task_id)
    return task_id


def test_required_current_admission_rejects_unbound_native_claim(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setenv("ANVER_CURRENT_ADMISSION_MODE", "required")
    with kb.connect(tmp_path / "kanban.db") as conn:
        task_id = _unbound_stale_task(conn)

        assert kb.claim_task(conn, task_id, claimer="stale-writer") is None
        assert kb.get_task(conn, task_id).status == "ready"
        events = kb.list_events(conn, task_id)
        rejection = [event for event in events if event.kind == "current_admission_rejected"][-1]
        assert rejection.payload == {
            "operation": "claim",
            "reason": "missing_current_contract_binding",
        }


def test_current_admission_can_be_enabled_from_existing_hermes_config(monkeypatch):
    monkeypatch.delenv("ANVER_CURRENT_ADMISSION_MODE", raising=False)
    monkeypatch.setattr(
        hermes_config,
        "load_config",
        lambda: {"kanban": {"current_admission": {"mode": "required"}}},
    )

    assert getattr(admission, "current_admission_required", lambda: False)() is True


def test_required_current_admission_rejects_unbound_direct_completion(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setenv("ANVER_CURRENT_ADMISSION_MODE", "required")
    with kb.connect(tmp_path / "kanban.db") as conn:
        task_id = _unbound_stale_task(conn)

        assert kb.complete_task(conn, task_id, summary="narrative PASS") is False
        assert kb.get_task(conn, task_id).status == "ready"
        events = kb.list_events(conn, task_id)
        rejection = [event for event in events if event.kind == "current_admission_rejected"][-1]
        assert rejection.payload == {
            "operation": "complete",
            "reason": "missing_current_contract_binding",
        }


def test_required_current_admission_rejects_malformed_current_contract(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setenv("ANVER_CURRENT_ADMISSION_MODE", "required")
    payload = {
        "schema_version": "anveros.hermes-task-payload.v1",
        "current_contract": {"state": "CURRENT"},
    }
    body = "unsafe\n\nANVER_HERMES_TASK_PAYLOAD=" + json.dumps(payload)
    with kb.connect(tmp_path / "kanban.db") as conn:
        task_id = kb.create_task(
            conn,
            title="malformed current task",
            body=body,
            assignee="writer",
            initial_status="blocked",
        )
        assert kb.unblock_task(conn, task_id)

        assert kb.claim_task(conn, task_id) is None
        rejection = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "current_admission_rejected"
        ][-1]
        assert rejection.payload == {
            "operation": "claim",
            "reason": "invalid_current_contract_binding",
        }


def test_required_current_admission_fails_closed_when_authority_source_is_unavailable(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setenv("ANVER_CURRENT_ADMISSION_MODE", "required")
    monkeypatch.setenv("ANVER_CURRENT_AUTHORITY_ROOT", str(tmp_path / "missing"))
    monkeypatch.setenv("ANVER_CURRENT_AUTHORITY_SHA", "a" * 40)
    contract = {
        "schema_version": "anveros.current-founder-contract.v1",
        "state": "CURRENT",
        "version": 1,
        "work_identity": "anver-work:cutover",
        "contract_digest": "b" * 64,
    }
    body = "unsafe\n\nANVER_HERMES_TASK_PAYLOAD=" + json.dumps(
        {"current_contract": contract}
    )
    with kb.connect(tmp_path / "kanban.db") as conn:
        task_id = kb.create_task(
            conn,
            title="authority missing",
            body=body,
            assignee="writer",
            initial_status="blocked",
        )
        assert kb.unblock_task(conn, task_id)

        assert kb.claim_task(conn, task_id) is None
        rejection = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "current_admission_rejected"
        ][-1]
        assert rejection.payload == {
            "operation": "claim",
            "reason": "authority_source_unavailable",
        }


def test_required_current_admission_allows_a_valid_bound_task(
    tmp_path: Path, monkeypatch,
):
    root, sha = _authority_repo(tmp_path)
    monkeypatch.setenv("ANVER_CURRENT_ADMISSION_MODE", "required")
    monkeypatch.setenv("ANVER_CURRENT_AUTHORITY_ROOT", str(root))
    monkeypatch.setenv("ANVER_CURRENT_AUTHORITY_SHA", sha)
    with kb.connect(tmp_path / "kanban.db") as conn:
        task_id = kb.create_task(
            conn,
            title="CURRENT bounded task",
            body=_bound_body(_contract(2, "c" * 64)),
            assignee="writer",
            initial_status="blocked",
        )
        assert kb.unblock_task(conn, task_id)

        claimed = kb.claim_task(conn, task_id, claimer="current-writer")
        assert claimed is not None
        assert claimed.status == "running"


def test_required_current_admission_rejects_a_superseded_bound_task(
    tmp_path: Path, monkeypatch,
):
    root, sha = _authority_repo(tmp_path)
    monkeypatch.setenv("ANVER_CURRENT_ADMISSION_MODE", "required")
    monkeypatch.setenv("ANVER_CURRENT_AUTHORITY_ROOT", str(root))
    monkeypatch.setenv("ANVER_CURRENT_AUTHORITY_SHA", sha)
    with kb.connect(tmp_path / "kanban.db") as conn:
        stale_id = kb.create_task(
            conn,
            title="superseded task",
            body=_bound_body(_contract(1, "1" * 64)),
            assignee="writer",
            initial_status="blocked",
        )
        current_id = kb.create_task(
            conn,
            title="current task",
            body=_bound_body(_contract(2, "2" * 64)),
            assignee="writer",
            initial_status="blocked",
        )
        assert kb.unblock_task(conn, stale_id)
        assert kb.unblock_task(conn, current_id)

        assert kb.claim_task(conn, stale_id) is None
        rejection = [
            event for event in kb.list_events(conn, stale_id)
            if event.kind == "current_admission_rejected"
        ][-1]
        assert rejection.payload == {
            "operation": "claim",
            "reason": "superseded_current_contract",
        }
        assert kb.claim_task(conn, current_id) is not None


def test_required_current_admission_rejects_writer_when_contract_is_read_only(
    tmp_path: Path, monkeypatch,
):
    root, sha = _authority_repo(tmp_path)
    monkeypatch.setenv("ANVER_CURRENT_ADMISSION_MODE", "required")
    monkeypatch.setenv("ANVER_CURRENT_AUTHORITY_ROOT", str(root))
    monkeypatch.setenv("ANVER_CURRENT_AUTHORITY_SHA", sha)
    contract = _contract(1, "3" * 64)
    contract["writer_execution_allowed"] = False
    with kb.connect(tmp_path / "kanban.db") as conn:
        task_id = kb.create_task(
            conn,
            title="read-only challenge writer",
            body=_bound_body(contract),
            assignee="writer",
            initial_status="blocked",
        )
        assert kb.unblock_task(conn, task_id)

        assert kb.claim_task(conn, task_id) is None
        rejection = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "current_admission_rejected"
        ][-1]
        assert rejection.payload == {
            "operation": "claim",
            "reason": "writer_execution_not_allowed",
        }


def test_required_current_admission_rejects_unbound_review_resume(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.delenv("ANVER_CURRENT_ADMISSION_MODE", raising=False)
    with kb.connect(tmp_path / "kanban.db") as conn:
        task_id = kb.create_task(
            conn,
            title="historical review resume",
            body="old session without CURRENT binding",
            assignee="writer",
            initial_status="blocked",
        )
        assert kb.unblock_task(conn, task_id)
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        assert kb.request_review(
            conn,
            task_id,
            reviewer="auditor",
            summary="old narrative",
            expected_run_id=claimed.current_run_id,
        )
        monkeypatch.setenv("ANVER_CURRENT_ADMISSION_MODE", "required")

        assert kb.claim_review_task(conn, task_id) is None
        rejection = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "current_admission_rejected"
        ][-1]
        assert rejection.payload == {
            "operation": "claim_review",
            "reason": "missing_current_contract_binding",
        }
