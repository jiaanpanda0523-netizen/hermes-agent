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


def _set_claude_truth_state(
    truth_path,
    *,
    route_enabled: bool,
    capability_state: str,
    effective_ready: bool,
    entitlement_state: str,
):
    payload = json.loads(truth_path.read_text(encoding="utf-8"))
    if route_enabled:
        overrides = {}
    else:
        overrides = payload["founder_entitlement_overrides"]
    desired = {
        "routing": {"claude_code": {"enabled": route_enabled}},
        "founder_entitlement_overrides": overrides,
    }
    payload["desired_state"] = desired
    payload["desired_state_sha256"] = _desired_sha256(desired)
    payload["founder_entitlement_overrides"] = overrides
    service = payload["services"][0]
    service["capability_state"] = capability_state
    service["effective_ready"] = effective_ready
    service["effective_capability"]["entitlement_state"] = entitlement_state
    service["effective_capability"]["access_reachable"] = effective_ready
    service["effective_capability"]["task_bound_probes"] = (
        {
            "repository_engineering": {
                "state": "passed",
                "task_class": "repository_engineering",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "ttl_seconds": 21600,
            }
        }
        if effective_ready
        else {}
    )
    truth_path.write_text(json.dumps(payload), encoding="utf-8")


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


def test_moa_cached_runtime_is_rechecked_after_truth_turns_disabled(
    monkeypatch, tmp_path
):
    truth_path = _configure_truth(monkeypatch, tmp_path)
    _set_claude_truth_state(
        truth_path,
        route_enabled=True,
        capability_state="READY",
        effective_ready=True,
        entitlement_state="enabled",
    )
    import agent.moa_loop as moa

    moa._runtime_cache.clear()
    slot = {"provider": "claude-max-meridian", "model": "claude-opus-5"}
    assert moa._slot_runtime(slot)["provider"] == "claude-max-meridian"
    assert moa._runtime_cache

    _set_claude_truth_state(
        truth_path,
        route_enabled=False,
        capability_state="DISABLED",
        effective_ready=False,
        entitlement_state="disabled",
    )
    with pytest.raises(Exception, match="entitlement_disabled"):
        moa._slot_runtime(slot)


def test_internally_inconsistent_ready_disabled_truth_fails_closed(
    monkeypatch, tmp_path
):
    truth_path = _configure_truth(monkeypatch, tmp_path)
    _set_claude_truth_state(
        truth_path,
        route_enabled=True,
        capability_state="DISABLED",
        effective_ready=True,
        entitlement_state="enabled",
    )
    from hermes_cli.runtime_provider import current_capability_admission

    admission = current_capability_admission("anthropic", "claude-sonnet-5")
    assert admission == {
        "available": False,
        "reason": "capability_truth_inconsistent",
    }


@pytest.mark.parametrize(
    "malformation",
    [
        "missing_routing",
        "routing_wrong_type",
        "missing_claude_route",
        "claude_route_wrong_type",
        "route_enabled_missing",
        "missing_overrides",
        "overrides_wrong_type",
        "claude_override_wrong_type",
        "empty_desired_state",
        "non_boolean_route_enabled",
        "generation_boolean",
        "duplicate_claude_service",
        "ready_missing_task_classes",
        "ready_missing_task_probes",
        "ready_empty_task_probes",
    ],
)
def test_structurally_malformed_self_hashed_truth_fails_closed(
    monkeypatch, tmp_path, malformation
):
    truth_path = _configure_truth(monkeypatch, tmp_path)
    _set_claude_truth_state(
        truth_path,
        route_enabled=True,
        capability_state="READY",
        effective_ready=True,
        entitlement_state="enabled",
    )
    payload = json.loads(truth_path.read_text(encoding="utf-8"))
    desired = payload["desired_state"]
    if malformation == "missing_routing":
        desired.pop("routing")
    elif malformation == "routing_wrong_type":
        desired["routing"] = []
    elif malformation == "missing_claude_route":
        desired["routing"].pop("claude_code")
    elif malformation == "claude_route_wrong_type":
        desired["routing"]["claude_code"] = []
    elif malformation == "route_enabled_missing":
        desired["routing"]["claude_code"].pop("enabled")
    elif malformation == "missing_overrides":
        desired.pop("founder_entitlement_overrides")
    elif malformation == "overrides_wrong_type":
        desired["founder_entitlement_overrides"] = []
        payload["founder_entitlement_overrides"] = []
    elif malformation == "claude_override_wrong_type":
        desired["founder_entitlement_overrides"]["claude"] = []
        payload["founder_entitlement_overrides"] = desired[
            "founder_entitlement_overrides"
        ]
    elif malformation == "empty_desired_state":
        desired.clear()
    elif malformation == "non_boolean_route_enabled":
        desired["routing"]["claude_code"]["enabled"] = "false"
    elif malformation == "generation_boolean":
        payload["desired_generation"] = True
    elif malformation == "duplicate_claude_service":
        payload["services"].append(dict(payload["services"][0]))
    elif malformation == "ready_missing_task_classes":
        payload["services"][0]["effective_capability"].pop(
            "supported_task_classes"
        )
    elif malformation == "ready_missing_task_probes":
        payload["services"][0]["effective_capability"].pop(
            "task_bound_probes"
        )
    elif malformation == "ready_empty_task_probes":
        payload["services"][0]["effective_capability"]["task_bound_probes"] = {}
    payload["desired_state_sha256"] = _desired_sha256(desired)
    truth_path.write_text(json.dumps(payload), encoding="utf-8")

    from hermes_cli.runtime_provider import current_capability_admission

    admission = current_capability_admission("anthropic", "claude-sonnet-5")
    assert admission["available"] is False
    assert admission["reason"] in {
        "capability_truth_structure_invalid",
        "desired_generation_missing_or_invalid",
    }


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


def test_cli_fallback_checks_target_model_before_assigning_it(monkeypatch, tmp_path):
    _configure_truth(monkeypatch, tmp_path)
    from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin

    stub = object.__new__(CLIAgentSetupMixin)
    stub.requested_provider = "claude-max-meridian"
    stub.provider = "local-qwen"
    stub.model = "qwen3-coder"
    stub.api_key = "no-key-required"
    stub.base_url = "http://127.0.0.1:1234/v1"
    stub.api_mode = "chat_completions"
    stub.acp_command = None
    stub.acp_args = []
    stub.agent = None
    stub._explicit_api_key = None
    stub._explicit_base_url = None
    stub._fallback_model = [
        {
            "provider": "local-qwen",
            "model": "anthropic/claude-sonnet-5",
        }
    ]
    stub._normalize_model_for_provider = lambda _provider: False

    assert stub._ensure_runtime_credentials() is False
    assert stub.model == "qwen3-coder"
    assert stub.requested_provider == "claude-max-meridian"


def test_cli_primary_checks_live_target_model(monkeypatch, tmp_path):
    _configure_truth(monkeypatch, tmp_path)
    from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin

    stub = object.__new__(CLIAgentSetupMixin)
    stub.requested_provider = "local-qwen"
    stub.provider = "local-qwen"
    stub.model = "anthropic/claude-sonnet-5"
    stub.api_key = "no-key-required"
    stub.base_url = "http://127.0.0.1:1234/v1"
    stub.api_mode = "chat_completions"
    stub.acp_command = None
    stub.acp_args = []
    stub.agent = None
    stub._explicit_api_key = None
    stub._explicit_base_url = None
    stub._fallback_model = []
    stub._normalize_model_for_provider = lambda _provider: False

    assert stub._ensure_runtime_credentials() is False
    assert stub.model == "anthropic/claude-sonnet-5"


def test_messaging_gateway_fallback_checks_target_model(monkeypatch, tmp_path):
    _configure_truth(monkeypatch, tmp_path)
    import gateway.run as gateway_run

    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_runtime_config",
        lambda: {
            "fallback_model": {
                "provider": "local-qwen",
                "model": "anthropic/claude-sonnet-5",
            }
        },
    )

    assert gateway_run._try_resolve_fallback_provider() is None


def test_messaging_gateway_final_runtime_identity_is_admitted(
    monkeypatch, tmp_path
):
    _configure_truth(monkeypatch, tmp_path)
    from gateway.run import _enforce_runtime_model_current_capability

    with pytest.raises(Exception, match="entitlement_disabled"):
        _enforce_runtime_model_current_capability(
            "anthropic/claude-sonnet-5",
            {"provider": "local-qwen", "requested_provider": "local-qwen"},
        )


def test_acp_agent_build_checks_effective_model_identity(monkeypatch, tmp_path):
    _configure_truth(monkeypatch, tmp_path)
    import run_agent
    from acp_adapter.session import SessionManager

    monkeypatch.setattr(run_agent, "AIAgent", lambda **kwargs: SimpleNamespace(**kwargs))

    manager = SessionManager(db=SimpleNamespace())
    with pytest.raises(Exception, match="entitlement_disabled"):
        manager._make_agent(
            session_id="acp-current-truth",
            cwd=str(tmp_path),
            model="anthropic/claude-sonnet-5",
            requested_provider="local-qwen",
        )


def test_delegation_direct_endpoint_cannot_swallow_policy_denial(
    monkeypatch, tmp_path
):
    _configure_truth(monkeypatch, tmp_path)
    from tools.delegate_tool import _resolve_delegation_credentials

    parent = SimpleNamespace(
        model="qwen3-coder",
        provider="local-qwen",
        api_key="no-key-required",
        base_url="http://127.0.0.1:1234/v1",
        api_mode="chat_completions",
        request_overrides=None,
    )
    with pytest.raises(Exception, match="entitlement_disabled"):
        _resolve_delegation_credentials(
            {
                "provider": "local-qwen",
                "model": "anthropic/claude-sonnet-5",
                "base_url": "http://127.0.0.1:1234/v1",
                "api_key": "no-key-required",
            },
            parent,
        )


def test_named_delegation_checks_runtime_supplied_default_model(
    monkeypatch, tmp_path
):
    _configure_truth(monkeypatch, tmp_path)
    import hermes_cli.runtime_provider as runtime_provider
    from tools.delegate_tool import _resolve_delegation_credentials

    monkeypatch.setattr(
        runtime_provider,
        "resolve_runtime_provider",
        lambda **_kwargs: {
            "provider": "custom",
            "requested_provider": "researcher",
            "model": "anthropic/claude-sonnet-5",
            "base_url": "http://127.0.0.1:1234/v1",
            "api_key": "no-key-required",
            "api_mode": "chat_completions",
        },
    )

    with pytest.raises(Exception, match="entitlement_disabled"):
        _resolve_delegation_credentials(
            {"provider": "researcher"},
            SimpleNamespace(
                model="qwen3-coder",
                provider="local-qwen",
                request_overrides=None,
            ),
        )


def test_long_lived_tui_agent_rechecks_truth_at_turn_boundary(
    monkeypatch, tmp_path
):
    _configure_truth(monkeypatch, tmp_path)
    from run_agent import AIAgent

    agent = object.__new__(AIAgent)
    agent.requested_provider = "local-qwen"
    agent.provider = "local-qwen"
    agent.model = "anthropic/claude-sonnet-5"
    with pytest.raises(Exception, match="entitlement_disabled"):
        agent.run_conversation("must stop before any retained client call")


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
