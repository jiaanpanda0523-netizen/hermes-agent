from __future__ import annotations

import contextlib
import json

from cron import scheduler


def test_agent_cron_without_current_binding_fails_before_agent_start(monkeypatch):
    monkeypatch.setenv("ANVER_CURRENT_ADMISSION_MODE", "required")
    job = {
        "id": "cron-stale-write",
        "name": "stale write-capable cron",
        "prompt": "Write a company report from an old resumable session.",
    }

    success, output, final_response, error = scheduler.run_job(job)

    assert success is False
    assert "FAIL_CLOSED_MISSING_CURRENT_CONTRACT" in output
    assert final_response == ""
    assert error == "FAIL_CLOSED_MISSING_CURRENT_CONTRACT"


def test_agent_cron_with_malformed_current_binding_fails_before_agent_start(monkeypatch):
    monkeypatch.setenv("ANVER_CURRENT_ADMISSION_MODE", "required")
    prompt = "write report\n\nANVER_HERMES_TASK_PAYLOAD=" + json.dumps(
        {"current_contract": {"state": "CURRENT"}}
    )
    job = {
        "id": "cron-malformed-current",
        "name": "malformed current cron",
        "prompt": prompt,
    }

    success, output, final_response, error = scheduler.run_job(job)

    assert success is False
    assert "FAIL_CLOSED_INVALID_CURRENT_CONTRACT" in output
    assert final_response == ""
    assert error == "FAIL_CLOSED_INVALID_CURRENT_CONTRACT"


def test_agent_cron_compares_against_cron_and_kanban_authority_bodies(monkeypatch):
    from cron import jobs as cron_jobs
    from hermes_cli import anver_current_admission, kanban_db

    seen = {}

    class _Rows:
        @staticmethod
        def fetchall():
            return [{"body": "kanban-current-v2"}]

    class _Connection:
        @staticmethod
        def execute(_query):
            return _Rows()

    @contextlib.contextmanager
    def _connect_closing():
        yield _Connection()

    def _reject(body, *, authority_bodies):
        seen["body"] = body
        seen["authority_bodies"] = authority_bodies
        return "superseded_current_contract"

    monkeypatch.setattr(anver_current_admission, "current_admission_required", lambda: True)
    monkeypatch.setattr(anver_current_admission, "current_admission_rejection", _reject)
    monkeypatch.setattr(
        cron_jobs,
        "load_jobs",
        lambda: [{"prompt": "cron-current-v2"}, {"prompt": ""}],
    )
    monkeypatch.setattr(kanban_db, "connect_closing", _connect_closing)

    success, output, final_response, error = scheduler.run_job(
        {
            "id": "cron-current-v1",
            "name": "superseded cron",
            "prompt": "cron-current-v1",
        }
    )

    assert success is False
    assert error == "FAIL_CLOSED_SUPERSEDED_CURRENT_CONTRACT"
    assert final_response == ""
    assert "FAIL_CLOSED_SUPERSEDED_CURRENT_CONTRACT" in output
    assert seen == {
        "body": "cron-current-v1",
        "authority_bodies": [
            "cron-current-v1",
            "cron-current-v2",
            "kanban-current-v2",
        ],
    }


def test_current_admission_rejection_never_delivers_or_resumes_old_session(monkeypatch):
    delivered = []
    marked = []
    finished = []
    job = {
        "id": "cron-stale-write",
        "name": "stale write-capable cron",
        "deliver": "bot-chat:forstnouslite",
    }

    monkeypatch.setattr(
        scheduler, "create_execution", lambda *_a, **_kw: {"id": "exec-stale"}
    )
    monkeypatch.setattr(scheduler, "claim_dispatch", lambda *_a, **_kw: True)
    monkeypatch.setattr(scheduler, "mark_execution_running", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        scheduler,
        "run_job",
        lambda *_a, **_kw: (
            False,
            "blocked before agent construction",
            "",
            "FAIL_CLOSED_MISSING_CURRENT_CONTRACT",
        ),
    )
    monkeypatch.setattr(scheduler, "save_job_output", lambda *_a, **_kw: "/tmp/blocked.md")
    monkeypatch.setattr(
        scheduler,
        "_deliver_result",
        lambda *args, **kwargs: delivered.append((args, kwargs)),
    )
    monkeypatch.setattr(
        scheduler,
        "mark_job_run",
        lambda *args, **kwargs: marked.append((args, kwargs)),
    )
    monkeypatch.setattr(
        scheduler,
        "finish_execution",
        lambda *args, **kwargs: finished.append((args, kwargs)),
    )

    assert scheduler.run_one_job(job) is True
    assert delivered == []
    assert marked == [
        (("cron-stale-write", False, "FAIL_CLOSED_MISSING_CURRENT_CONTRACT"),
         {"delivery_error": None})
    ]
    assert finished == [
        (("exec-stale",), {
            "success": False,
            "error": "FAIL_CLOSED_MISSING_CURRENT_CONTRACT",
            "delivery_outcome": "suppressed",
        })
    ]
