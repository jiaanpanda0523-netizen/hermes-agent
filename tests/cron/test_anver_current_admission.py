from __future__ import annotations

from cron import scheduler
import json


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
