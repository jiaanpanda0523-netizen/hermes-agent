"""Behavior tests for native provider disablement in auxiliary routing."""

import asyncio
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


def test_same_credential_isolated_between_disabled_internal_and_allowed_customer_home(
    tmp_path, monkeypatch
):
    internal_home = tmp_path / "internal-home"
    internal_home.mkdir()
    (internal_home / "config.yaml").write_text(
        json.dumps({"providers": {"deepseek": {"enabled": False}}}),
        encoding="utf-8",
    )
    customer_home = tmp_path / "anmoni-customer-home"
    customer_home.mkdir()
    (customer_home / "config.yaml").write_text(
        json.dumps({"providers": {"deepseek": {"enabled": True}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "same-fake-credential")

    from agent.auxiliary_client import resolve_provider_client

    fake_client = MagicMock()
    with patch(
        "agent.auxiliary_client._create_openai_client",
        return_value=fake_client,
    ) as create_client:
        monkeypatch.setenv("HERMES_HOME", str(internal_home))
        internal_client, _ = resolve_provider_client(
            "deepseek",
            model="boundary-test-model",
            explicit_base_url="https://api.deepseek.com/v1",
            explicit_api_key="same-fake-credential",
        )
        monkeypatch.setenv("HERMES_HOME", str(customer_home))
        customer_client, _ = resolve_provider_client(
            "deepseek",
            model="boundary-test-model",
            explicit_base_url="https://api.deepseek.com/v1",
            explicit_api_key="same-fake-credential",
        )

    assert internal_client is None
    assert customer_client is fake_client
    create_client.assert_called_once()


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


def _auto_discovery_config(deepseek_enabled):
    return {
        "model": {"provider": "auto", "default": ""},
        "providers": {
            "deepseek": {"enabled": deepseek_enabled},
            "openrouter": {"enabled": False},
            "nous": {"enabled": False},
            "custom": {"enabled": False},
        },
    }


def _fake_api_key_credentials(provider_id):
    if provider_id == "deepseek":
        return {
            "api_key": "present-but-fake",
            "base_url": "https://api.deepseek.com/v1",
        }
    return {}


def _fake_aux_model(provider_id, **_kwargs):
    return "boundary-test-model" if provider_id == "deepseek" else ""


def test_sync_auto_cache_entry_is_evicted_after_concrete_provider_is_disabled(
    tmp_path, monkeypatch
):
    hermes_home = _write_config(
        tmp_path,
        monkeypatch,
        _auto_discovery_config(True),
    )

    from agent.auxiliary_client import _client_cache, _get_cached_client

    cached_client = MagicMock()
    cached_client.close = MagicMock(return_value=None)
    with (
        patch(
            "hermes_cli.auth.resolve_api_key_provider_credentials",
            side_effect=_fake_api_key_credentials,
        ),
        patch(
            "agent.auxiliary_client._get_aux_model_for_provider",
            side_effect=_fake_aux_model,
        ),
        patch(
            "agent.auxiliary_client._create_openai_client",
            return_value=cached_client,
        ) as create_client,
    ):
        first_client, _ = _get_cached_client(
            "auto",
            main_runtime={"provider": "auto", "model": ""},
            task="compression",
        )
        (hermes_home / "config.yaml").write_text(
            json.dumps(_auto_discovery_config(False)),
            encoding="utf-8",
        )
        second_client, second_model = _get_cached_client(
            "auto",
            main_runtime={"provider": "auto", "model": ""},
            task="compression",
        )

    assert first_client is cached_client
    assert getattr(first_client, "_hermes_aux_effective_provider") == "deepseek"
    assert second_client is None
    assert second_model is None
    create_client.assert_called_once()
    cached_client.close.assert_called_once()
    assert not _client_cache


def test_async_auto_cache_entry_is_evicted_when_effective_provider_is_disabled(
    tmp_path, monkeypatch
):
    hermes_home = _write_config(
        tmp_path,
        monkeypatch,
        _auto_discovery_config(True),
    )

    from agent.auxiliary_client import _client_cache, _get_cached_client

    async def exercise_transition():
        sync_client = MagicMock()
        async_client = MagicMock()
        async_client.close = MagicMock(return_value=None)
        with (
            patch(
                "hermes_cli.auth.resolve_api_key_provider_credentials",
                side_effect=_fake_api_key_credentials,
            ),
            patch(
                "agent.auxiliary_client._get_aux_model_for_provider",
                side_effect=_fake_aux_model,
            ),
            patch(
                "agent.auxiliary_client._create_openai_client",
                return_value=sync_client,
            ) as create_client,
            patch(
                "agent.auxiliary_client._to_async_client",
                side_effect=lambda _client, model, **_kwargs: (async_client, model),
            ),
        ):
            first_client, _ = _get_cached_client(
                "auto",
                async_mode=True,
                main_runtime={"provider": "auto", "model": ""},
                task="compression",
            )
            assert first_client is async_client
            assert getattr(first_client, "_hermes_aux_effective_provider") == "deepseek"

            (hermes_home / "config.yaml").write_text(
                json.dumps(_auto_discovery_config(False)),
                encoding="utf-8",
            )
            second_client, second_model = _get_cached_client(
                "auto",
                async_mode=True,
                main_runtime={"provider": "auto", "model": ""},
                task="compression",
            )

        assert second_client is None
        assert second_model is None
        create_client.assert_called_once()
        async_client.close.assert_called_once()
        assert not _client_cache

    asyncio.run(exercise_transition())


def test_auto_local_custom_cache_uses_native_custom_identity(
    tmp_path, monkeypatch
):
    config = _auto_discovery_config(False)
    config["providers"]["custom"]["enabled"] = True
    hermes_home = _write_config(tmp_path, monkeypatch, config)

    from agent.auxiliary_client import _client_cache, _get_cached_client

    cached_client = MagicMock()
    cached_client.close = MagicMock(return_value=None)
    with (
        patch(
            "agent.auxiliary_client._try_custom_endpoint",
            return_value=(cached_client, "local-test-model"),
        ) as resolve_custom,
        patch(
            "agent.auxiliary_client._resolve_api_key_provider",
            return_value=(None, None),
        ),
    ):
        first_client, _ = _get_cached_client(
            "auto",
            main_runtime={"provider": "auto", "model": ""},
            task="compression",
        )
        config["providers"]["custom"]["enabled"] = False
        (hermes_home / "config.yaml").write_text(
            json.dumps(config), encoding="utf-8"
        )
        second_client, _ = _get_cached_client(
            "auto",
            main_runtime={"provider": "auto", "model": ""},
            task="compression",
        )

    assert first_client is cached_client
    assert getattr(first_client, "_hermes_aux_effective_provider") == "custom"
    assert second_client is None
    resolve_custom.assert_called_once()
    cached_client.close.assert_called_once()
    assert not _client_cache


def test_quota_failure_does_not_activate_disabled_aux_fallback(
    tmp_path, monkeypatch
):
    _write_config(
        tmp_path,
        monkeypatch,
        {
            "model": {"provider": "openai-codex", "default": "gpt-test"},
            "providers": {
                "deepseek": {"enabled": False},
                "custom": {"enabled": False},
            },
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
                        },
                        {
                            "provider": "custom",
                            "model": "local-test-model",
                            "base_url": "http://127.0.0.1:11434/v1",
                        },
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
    blocked_network_constructors = []

    def record_blocked_constructor(*args, **kwargs):
        blocked_network_constructors.append(kwargs.get("base_url", ""))
        raise AssertionError("disabled fallback constructed a client")

    with (
        patch(
            "agent.auxiliary_client._get_cached_client",
            return_value=(primary_client, "gpt-test"),
        ),
        patch(
            "agent.auxiliary_client._create_openai_client",
            side_effect=record_blocked_constructor,
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
    assert blocked_network_constructors == []
