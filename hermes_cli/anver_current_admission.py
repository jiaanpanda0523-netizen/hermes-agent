"""Thin loader for Anver's already-approved CURRENT contract authority."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path
from types import ModuleType


class AuthoritySourceUnavailable(RuntimeError):
    pass


def _current_admission_config() -> dict:
    try:
        from hermes_cli.config import load_config

        value = (load_config().get("kanban") or {}).get("current_admission") or {}
    except Exception:
        value = {}
    return value if isinstance(value, dict) else {}


def current_admission_required() -> bool:
    mode = os.environ.get("ANVER_CURRENT_ADMISSION_MODE")
    if mode is None:
        mode = _current_admission_config().get("mode", "off")
    return str(mode).strip().lower() == "required"


def _configured_authority() -> tuple[Path, str]:
    config = _current_admission_config()
    root = Path(
        os.environ.get("ANVER_CURRENT_AUTHORITY_ROOT")
        or str(config.get("authority_root") or "")
    ).expanduser()
    sha = str(
        os.environ.get("ANVER_CURRENT_AUTHORITY_SHA")
        or config.get("authority_sha")
        or ""
    ).strip().lower()
    if not root.is_dir() or len(sha) != 40:
        raise AuthoritySourceUnavailable("authority root or SHA is not configured")
    return root.resolve(), sha


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AuthoritySourceUnavailable("authority git probe failed") from exc
    if result.returncode != 0:
        raise AuthoritySourceUnavailable("authority git probe was rejected")
    return result.stdout.strip()


def load_founder_intent_authority() -> ModuleType:
    root, expected_sha = _configured_authority()
    if _git(root, "rev-parse", "HEAD").lower() != expected_sha:
        raise AuthoritySourceUnavailable("authority checkout is not at the configured SHA")
    source = root / "control_plane" / "founder_intent.py"
    if not source.is_file():
        raise AuthoritySourceUnavailable("FounderIntent authority source is missing")
    expected_blob = _git(root, "rev-parse", f"{expected_sha}:control_plane/founder_intent.py")
    if _git(root, "hash-object", str(source)) != expected_blob:
        raise AuthoritySourceUnavailable("FounderIntent authority source differs from the configured SHA")
    spec = importlib.util.spec_from_file_location(
        f"_anver_founder_intent_{expected_sha}", source
    )
    if spec is None or spec.loader is None:
        raise AuthoritySourceUnavailable("FounderIntent authority source cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise AuthoritySourceUnavailable("FounderIntent authority source failed to load") from exc
    if not callable(getattr(module, "validate_current_contract", None)):
        raise AuthoritySourceUnavailable("FounderIntent validator is unavailable")
    return module


def validate_current_contract(contract: dict) -> None:
    authority = load_founder_intent_authority()
    authority.validate_current_contract(contract)


def current_admission_rejection(
    body: object,
    *,
    authority_bodies: list[object] | None = None,
) -> str | None:
    """Return a stable fail-closed reason, or ``None`` when CURRENT admits."""
    if not isinstance(body, str) or "ANVER_HERMES_TASK_PAYLOAD=" not in body:
        return "missing_current_contract_binding"
    try:
        payload = json.loads(body.rsplit("ANVER_HERMES_TASK_PAYLOAD=", 1)[1])
    except (TypeError, json.JSONDecodeError):
        return "invalid_current_contract_binding"
    contract = payload.get("current_contract") if isinstance(payload, dict) else None
    if (
        not isinstance(contract, dict)
        or contract.get("schema_version") != "anveros.current-founder-contract.v1"
        or contract.get("state") != "CURRENT"
        or not isinstance(contract.get("contract_digest"), str)
        or not isinstance(contract.get("work_identity"), str)
        or not isinstance(contract.get("version"), int)
    ):
        return "invalid_current_contract_binding"
    try:
        validate_current_contract(contract)
    except AuthoritySourceUnavailable:
        return "authority_source_unavailable"
    except (TypeError, ValueError):
        return "invalid_current_contract_binding"
    if (
        payload.get("schema_version") != "anveros.hermes-task-payload.v1"
        or payload.get("execution_authority") != "hermes_kanban"
        or payload.get("contract_digest") != contract["contract_digest"]
        or payload.get("contract_version") != contract["version"]
        or payload.get("work_identity") != contract["work_identity"]
        or payload.get("writer_execution_allowed")
        != contract.get("writer_execution_allowed")
        or payload.get("kind") not in {"worker", "verifier"}
    ):
        return "invalid_current_contract_binding"
    if (
        payload.get("kind") == "worker"
        and contract.get("writer_execution_allowed") is not True
    ):
        return "writer_execution_not_allowed"

    versions: list[tuple[int, str]] = []
    for candidate_body in authority_bodies or [body]:
        if not isinstance(candidate_body, str) or "ANVER_HERMES_TASK_PAYLOAD=" not in candidate_body:
            continue
        try:
            candidate_payload = json.loads(
                candidate_body.rsplit("ANVER_HERMES_TASK_PAYLOAD=", 1)[1]
            )
        except (TypeError, json.JSONDecodeError):
            continue
        candidate_contract = (
            candidate_payload.get("current_contract")
            if isinstance(candidate_payload, dict)
            else None
        )
        if (
            isinstance(candidate_contract, dict)
            and candidate_contract.get("work_identity") == contract["work_identity"]
            and isinstance(candidate_contract.get("version"), int)
            and isinstance(candidate_contract.get("contract_digest"), str)
        ):
            versions.append(
                (candidate_contract["version"], candidate_contract["contract_digest"])
            )
    if versions:
        latest_version = max(version for version, _ in versions)
        latest_digests = {
            digest for version, digest in versions if version == latest_version
        }
        if len(latest_digests) != 1:
            return "competing_current_contracts"
        if (
            contract["version"] != latest_version
            or contract["contract_digest"] not in latest_digests
        ):
            return "superseded_current_contract"
    return None
