"""Behavior tests for native provider disablement in auxiliary routing."""

import json
from unittest.mock import MagicMock, patch

import pytest


def _write_config(tmp_path, monkeypatch, config):
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    return hermes_home


@pytest.mark.parametrize(
    ("provider", "provider_block", "base_url"),
    [
        ("deepseek", "deepseek", "https://api.deepseek.com/v1"),
        ("custom", "custom", "https://disabled.example/v1"),
    ],
)
def test_explicit_disabled_provider_never_constructs_client(
    tmp_path, monkeypatch, provider, provider_block, base_url
):
    _write_config(
        tmp_path,
        monkeypatch,
        {"providers": {provider_block: {"enabled": False}}},
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "present-but-disabled")

    from agent.auxiliary_client import resolve_provider_client

    with patch(
        "agent.auxiliary_client._create_openai_client",
        side_effect=AssertionError("disabled provider reached network client construction"),
    ) as create_client:
        client, model = resolve_provider_client(
            provider,
            model="boundary-test-model",
            explicit_base_url=base_url,
            explicit_api_key="present-but-disabled",
        )

    assert (client, model) == (None, None)
    create_client.assert_not_called()


def test_api_key_auto_discovery_skips_disabled_provider_with_key_present(
    tmp_path, monkeypatch
):
    _write_config(
        tmp_path,
        monkeypatch,
        {"providers": {"deepseek": {"enabled": False}}},
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "present-but-disabled")

    from agent.auxiliary_client import _resolve_api_key_provider

    with patch(
        "agent.auxiliary_client._create_openai_client",
        side_effect=AssertionError("disabled API-key provider constructed a client"),
    ) as create_client:
        client, model = _resolve_api_key_provider()

    assert (client, model) == (None, None)
    create_client.assert_not_called()


def test_auto_route_skips_disabled_main_and_discovery_candidates(
    tmp_path, monkeypatch
):
    _write_config(
        tmp_path,
        monkeypatch,
        {
            "model": {"provider": "deepseek", "default": "boundary-test-model"},
            "providers": {
                "deepseek": {"enabled": False},
                "openrouter": {"enabled": False},
                "nous": {"enabled": False},
                "custom": {"enabled": False},
            },
        },
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "present-but-disabled")

    from agent.auxiliary_client import resolve_provider_client

    with patch(
        "agent.auxiliary_client._create_openai_client",
        side_effect=AssertionError("disabled auto candidate constructed a client"),
    ) as create_client:
        client, model = resolve_provider_client(
            "auto",
            task="compression",
            main_runtime={
                "provider": "deepseek",
                "model": "boundary-test-model",
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "present-but-disabled",
            },
        )

    assert (client, model) == (None, None)
    create_client.assert_not_called()


@pytest.mark.parametrize(
    ("provider", "resolver"),
    [
        ("openrouter", "_try_openrouter"),
        ("anthropic", "_try_anthropic"),
        ("custom", "_try_custom_endpoint"),
    ],
)
def test_strict_vision_route_skips_disabled_provider(
    tmp_path, monkeypatch, provider, resolver
):
    _write_config(
        tmp_path,
        monkeypatch,
        {"providers": {provider: {"enabled": False}}},
    )

    from agent.auxiliary_client import _resolve_strict_vision_backend

    with patch(
        f"agent.auxiliary_client.{resolver}",
        side_effect=AssertionError("disabled vision provider was resolved"),
    ) as resolve_backend:
        client, model = _resolve_strict_vision_backend(provider)

    assert (client, model) == (None, None)
    resolve_backend.assert_not_called()


def test_quota_failure_does_not_activate_disabled_aux_fallback(
    tmp_path, monkeypatch
):
    _write_config(
        tmp_path,
        monkeypatch,
        {
            "model": {"provider": "openai-codex", "default": "gpt-test"},
            "providers": {"deepseek": {"enabled": False}},
            "auxiliary": {
                "compression": {
                    "provider": "openai-codex",
                    "model": "gpt-test",
                    "fallback_chain": [
                        {
                            "provider": "deepseek",
                            "model": "boundary-test-model",
                            "base_url": "https://api.deepseek.com/v1",
                            "api_key": "present-but-disabled",
                        }
                    ],
                }
            },
        },
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "present-but-disabled")

    from agent.auxiliary_client import call_llm

    quota_error = Exception("Payment Required: insufficient credits")
    quota_error.status_code = 402
    primary_client = MagicMock()
    primary_client.base_url = "https://allowed-primary.example/v1"
    primary_client.chat.completions.create.side_effect = quota_error

    with (
        patch(
            "agent.auxiliary_client._get_cached_client",
            return_value=(primary_client, "gpt-test"),
        ),
        patch(
            "agent.auxiliary_client._create_openai_client",
            side_effect=AssertionError("disabled fallback constructed a client"),
        ) as create_client,
        patch(
            "agent.auxiliary_client._try_main_agent_model_fallback",
            return_value=(None, None, ""),
        ),
        patch(
            "agent.auxiliary_client._recoverable_pool_provider",
            return_value=None,
        ),
    ):
        with pytest.raises(Exception, match="insufficient credits"):
            call_llm(
                task="compression",
                messages=[{"role": "user", "content": "compress"}],
            )

    primary_client.chat.completions.create.assert_called_once()
    create_client.assert_not_called()
