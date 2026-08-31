"""Current capability truth supersedes historical Claude evidence.

These behavior tests deliberately keep old Claude routes, aliases, session
metadata, and binary/health evidence present.  A configured canonical truth
snapshot is the only authority deciding whether those historical artifacts
are usable now.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace

import pytest


def _desired_sha256(value: dict) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _configure_truth(
    monkeypatch,
    tmp_path,
    *,
    malformed: bool = False,
    age: timedelta = timedelta(),
):
    home = tmp_path / "hermes-home"
    home.mkdir()
    truth_path = tmp_path / "ANVER_AI_CAPABILITY_TRUTH_V1.json"
    if malformed:
        truth_path.write_text("{not-json", encoding="utf-8")
    else:
        overrides = {
            "claude": {
                "state": "disabled",
                "directive_id": "knife3-test",
                "reason": "founder_current_truth",
            },
            "claude_code": {
                "state": "disabled",
                "directive_id": "knife3-test",
                "reason": "founder_current_truth",
            },
        }
        desired = {
            "routing": {"claude_code": {"enabled": False}},
            "founder_entitlement_overrides": overrides,
        }
        generated_at = datetime.now(timezone.utc) - age
        truth_path.write_text(
            json.dumps(
                {
                    "schema_version": "anveros.ai_capability_truth.v1",
                    "generated_at_kst": generated_at.isoformat(),
                    "desired_generation": 3,
                    "desired_state": desired,
                    "desired_state_sha256": _desired_sha256(desired),
                    "founder_entitlement_overrides": overrides,
                    "services": [
                        {
                            "name": "claude",
                            # Historical/install evidence must not revive it.
                            "binary_present": True,
                            "historical_real_call_evidence_count": 42,
                            "effective_capability": {
                                "entitlement_state": "disabled",
                                "access_reachable": False,
                                "supported_task_classes": [
                                    "repository_engineering"
                                ],
                                "task_bound_probes": {},
                            },
                            "capability_state": "DISABLED",
                            "effective_ready": False,
                            "effective_reason": "entitlement_disabled",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    config = {
        "model": {"provider": "local-qwen", "default": "qwen3-coder"},
        "model_catalog": {
            "capability_truth_file": str(truth_path),
            "capability_truth_max_age_seconds": 21600,
            # A legacy positive health signal is evidence, not authority.
            "legacy_health": {"claude": True},
        },
        "providers": {
            "claude-max-meridian": {
                "name": "Historical Claude bridge",
                "base_url": "http://127.0.0.1:3456/v1",
                "api_key": "test-claude-key-long-enough",
                "model": "claude-opus-5",
            },
            "local-qwen": {
                "name": "Local Qwen",
                "base_url": "http://127.0.0.1:1234/v1",
                "api_key": "no-key-required",
                "model": "qwen3-coder",
            },
        },
        "model_aliases": {
            "sonnet": {
                "provider": "claude-max-meridian",
                "model": "claude-sonnet-5",
            }
        },
        "fallback_model": {
            "provider": "local-qwen",
            "model": "qwen3-coder",
        },
        "moa": {
            "presets": {
                "historical-claude": {
                    "reference_models": [
                        {
                            "provider": "claude-max-meridian",
                            "model": "claude-sonnet-5",
                        }
                    ],
                    "aggregator": {
                        "provider": "claude-max-meridian",
                        "model": "claude-opus-5",
                    },
                }
            }
        },
    }
    (home / "config.yaml").write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    return truth_path


def test_direct_and_alias_shaped_claude_routes_are_denied_by_current_truth(
    monkeypatch, tmp_path
):
    _configure_truth(monkeypatch, tmp_path)
    from hermes_cli.runtime_provider import resolve_runtime_provider

    for provider, model in (
        ("claude-max-meridian", "claude-opus-5"),
        ("anthropic", "claude-sonnet-5"),
        ("openrouter", "anthropic/claude-sonnet-5"),
    ):
        with pytest.raises(Exception, match="entitlement_disabled"):
            resolve_runtime_provider(requested=provider, target_model=model)


@pytest.mark.parametrize(
    ("malformed", "age", "reason"),
    [
        (True, timedelta(), "capability_truth_unreadable"),
        (False, timedelta(days=2), "capability_truth_stale"),
    ],
)
def test_bad_current_truth_fails_closed_only_for_claude(
    monkeypatch, tmp_path, malformed, age, reason
):
    _configure_truth(monkeypatch, tmp_path, malformed=malformed, age=age)
    from hermes_cli.runtime_provider import resolve_runtime_provider

    with pytest.raises(Exception, match=reason):
        resolve_runtime_provider(
            requested="claude-max-meridian", target_model="claude-opus-5"
        )

    safe = resolve_runtime_provider(
        requested="local-qwen", target_model="qwen3-coder"
    )
    assert safe["requested_provider"] == "local-qwen"


def test_picker_removes_claude_providers_and_models_but_keeps_other_models(
    monkeypatch, tmp_path
):
    _configure_truth(monkeypatch, tmp_path)
    from hermes_cli.inventory import build_models_payload, load_picker_context
    import hermes_cli.model_switch as model_switch

    rows = [
        {
            "slug": "anthropic",
            "name": "Anthropic",
            "models": ["claude-sonnet-5"],
            "total_models": 1,
            "source": "hermes",
        },
        {
            "slug": "claude-max-meridian",
            "name": "Historical Claude bridge",
            "models": ["claude-opus-5"],
            "total_models": 1,
            "source": "user-config",
            "is_user_defined": True,
        },
        {
            "slug": "openrouter",
            "name": "OpenRouter",
            "models": ["anthropic/claude-sonnet-5", "qwen/qwen3-coder"],
            "total_models": 2,
            "source": "built-in",
        },
        {
            "slug": "local-qwen",
            "name": "Local Qwen",
            "models": ["qwen3-coder"],
            "total_models": 1,
            "source": "user-config",
            "is_user_defined": True,
        },
    ]
    monkeypatch.setattr(model_switch, "list_authenticated_providers", lambda **_: rows)

    payload = build_models_payload(load_picker_context())
    by_slug = {row["slug"]: row for row in payload["providers"]}

    assert "anthropic" not in by_slug
    assert "claude-max-meridian" not in by_slug
    assert by_slug["openrouter"]["models"] == ["qwen/qwen3-coder"]
    assert by_slug["local-qwen"]["models"] == ["qwen3-coder"]


def test_moa_cannot_fall_open_to_a_bare_claude_slot(monkeypatch, tmp_path):
    _configure_truth(monkeypatch, tmp_path)
    import agent.moa_loop as moa

    moa._runtime_cache.clear()
    with pytest.raises(Exception, match="entitlement_disabled"):
        moa._slot_runtime(
            {"provider": "claude-max-meridian", "model": "claude-opus-5"}
        )
    assert moa._runtime_cache == {}


def test_alias_switch_is_rejected_before_ambient_credentials_can_continue(
    monkeypatch, tmp_path
):
    _configure_truth(monkeypatch, tmp_path)
    from hermes_cli.model_switch import switch_model
    from hermes_cli.config import load_config

    cfg = load_config()
    result = switch_model(
        raw_input="sonnet",
        current_provider="local-qwen",
        current_model="qwen3-coder",
        current_base_url="http://127.0.0.1:1234/v1",
        current_api_key="no-key-required",
        explicit_provider="claude-max-meridian",
        user_providers=cfg["providers"],
        custom_providers=[],
    )

    assert result.success is False
    assert "entitlement_disabled" in result.error_message


def test_disabled_primary_advances_to_non_claude_fallback(monkeypatch, tmp_path):
    _configure_truth(monkeypatch, tmp_path)
    from tui_gateway.server import _resolve_runtime_with_fallback

    result = _resolve_runtime_with_fallback(
        {
            "requested": "claude-max-meridian",
            "target_model": "claude-opus-5",
        }
    )

    assert result.used_fallback is True
    assert result.selected_model == "qwen3-coder"
    assert result.runtime["requested_provider"] == "local-qwen"


def test_cli_resume_keeps_ambient_runtime_when_history_used_claude(
    monkeypatch, tmp_path
):
    _configure_truth(monkeypatch, tmp_path)
    import cli as cli_mod

    calls = []
    stub = object.__new__(cli_mod.HermesCLI)
    stub.model = "qwen3-coder"
    stub.provider = "local-qwen"
    stub.requested_provider = "local-qwen"
    stub.base_url = "http://127.0.0.1:1234/v1"
    stub.api_key = "no-key-required"
    stub.api_mode = "chat_completions"
    stub._explicit_model_override = False
    stub._console_print = lambda *_: None
    stub.agent = SimpleNamespace(switch_model=lambda **kwargs: calls.append(kwargs))

    stub._restore_session_model(
        {
            "model": "claude-opus-5",
            "model_config": json.dumps(
                {
                    "gateway_runtime": {
                        "provider": "claude-max-meridian",
                        "base_url": "http://127.0.0.1:3456/v1",
                        "api_mode": "chat_completions",
                    }
                }
            ),
        }
    )

    assert stub.model == "qwen3-coder"
    assert stub.provider == "local-qwen"
    assert calls == []


def test_tui_resume_drops_currently_disabled_claude_runtime_metadata(
    monkeypatch, tmp_path
):
    _configure_truth(monkeypatch, tmp_path)
    from tui_gateway.server import _stored_session_runtime_overrides

    row = {
        "model": "claude-opus-5",
        "model_config": json.dumps(
            {
                "provider": "claude-max-meridian",
                "base_url": "http://127.0.0.1:3456/v1",
                "api_mode": "chat_completions",
            }
        ),
    }
    assert _stored_session_runtime_overrides(row) == {}


def test_archived_claude_history_remains_searchable(tmp_path):
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session(
            "historical-claude",
            "cli",
            model="claude-opus-5",
            model_config=json.dumps({"provider": "claude-max-meridian"}),
        )
        db.append_message(
            "historical-claude",
            "assistant",
            "Claude historical evidence stays searchable after current disable.",
        )
        assert db.set_session_archived("historical-claude", True)

        rows = db.search_messages("Claude")
        assert any(row["session_id"] == "historical-claude" for row in rows)
    finally:
        db.close()
