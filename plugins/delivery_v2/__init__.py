"""Delivery OS V2 policy on Hermes' typed Kanban completion boundary.

This is intentionally a small policy plugin, not a task store, dispatcher or
command parser. Hermes Kanban remains the only durable task authority; the
plugin only evaluates the task snapshot and proposed completion metadata that
the authoritative kernel passes to it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse


PRODUCTION_CHANGE = "PRODUCTION_CHANGE"
NON_PRODUCTION_DELIVERABLE = "NON_PRODUCTION_DELIVERABLE"
_TASK_CLASSES = {PRODUCTION_CHANGE, NON_PRODUCTION_DELIVERABLE}
_REQUIRED_RECEIPT = (
    "MERGED_SHA",
    "DEPLOYED_SHA",
    "PRODUCTION_URL",
    "DEPLOYMENT_STATUS",
    "EXACT_SHA_MATCH",
    "PRODUCTION_ACCEPTANCE_PROBE",
    "USER_VISIBLE_DELTA_EVIDENCE",
    "ROLLBACK_OR_REVERT_PATH",
)
_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?$")


def _identity(value: Any) -> str:
    return value.strip().casefold() if isinstance(value, str) else ""


def _contract(task: Any) -> dict[str, Any] | None:
    body = getattr(task, "body", None)
    if not isinstance(body, str):
        return None
    try:
        document = json.loads(body)
    except json.JSONDecodeError:
        return None
    value = document.get("delivery_v2") if isinstance(document, dict) else None
    return dict(value) if isinstance(value, dict) else None


def _url_is_https(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme == "https" and bool(parsed.netloc)


def before_kanban_task_complete(
    *, task: Any, actor: str, metadata: Any, **_: Any,
) -> dict[str, Any] | None:
    """Allow legacy cards; fail closed for explicitly enrolled V2 cards."""
    contract = _contract(task)
    if contract is None:
        return None
    issues: list[str] = []
    classification = contract.get("classification")
    if classification not in _TASK_CLASSES:
        issues.append("TASK_CLASSIFICATION_REQUIRED")
    if not _identity(contract.get("role")):
        issues.append("ROLE_REQUIRED")
    if classification == NON_PRODUCTION_DELIVERABLE:
        return _decision(issues)
    if classification != PRODUCTION_CHANGE:
        return _decision(issues or ["TASK_CLASSIFICATION_REQUIRED"])

    if getattr(task, "status", None) != "review":
        issues.append("INDEPENDENT_REVIEW_STATE_REQUIRED")
    if contract.get("state") != "PRODUCTION_VERIFY":
        issues.append("PRODUCTION_VERIFY_STATE_REQUIRED")
    receipt = metadata.get("production_receipt") if isinstance(metadata, Mapping) else None
    if not isinstance(receipt, Mapping):
        issues.append("PRODUCTION_RECEIPT_REQUIRED")
        receipt = {}
    for field in _REQUIRED_RECEIPT:
        if not _identity(receipt.get(field)):
            issues.append(f"{field}_REQUIRED")
    for field in ("MERGED_SHA", "DEPLOYED_SHA"):
        value = receipt.get(field)
        if not isinstance(value, str) or _SHA.fullmatch(value) is None:
            issues.append(f"{field}_INVALID")
    if not _url_is_https(receipt.get("PRODUCTION_URL")):
        issues.append("PRODUCTION_URL_INVALID")
    if receipt.get("DEPLOYMENT_STATUS") != "SUCCESS":
        issues.append("DEPLOYMENT_STATUS_SUCCESS_REQUIRED")
    for field in ("EXACT_SHA_MATCH", "PRODUCTION_ACCEPTANCE_PROBE"):
        if receipt.get(field) != "PASS":
            issues.append(f"{field}_PASS_REQUIRED")
    if receipt.get("MERGED_SHA") != receipt.get("DEPLOYED_SHA"):
        issues.append("EXACT_SHA_MATCH_FAILED")

    implementer = _identity(contract.get("implementer"))
    verifier = _identity(receipt.get("VERIFIER"))
    if not implementer:
        issues.append("IMPLEMENTER_REQUIRED")
    if not verifier:
        issues.append("VERIFIER_REQUIRED")
    if not implementer or not verifier or implementer == verifier or _identity(actor) != verifier:
        issues.append("INDEPENDENT_PRODUCTION_VERIFIER_REQUIRED")
    return _decision(issues)


def _decision(issues: list[str]) -> dict[str, Any]:
    unique = sorted(set(issues))
    if not unique:
        return {"allow": True}
    return {"allow": False, "reason": "delivery_v2:" + ",".join(unique)}


def register(ctx) -> None:
    """Register only at the native typed completion hook."""
    ctx.register_hook("before_kanban_task_complete", before_kanban_task_complete)
