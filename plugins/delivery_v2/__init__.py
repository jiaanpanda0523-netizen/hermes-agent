"""Delivery OS V2 policy on Hermes' typed Kanban completion boundary.

This is intentionally a small policy plugin, not a task store, dispatcher or
command parser. Hermes Kanban remains the only durable task authority; the
plugin only evaluates the task snapshot and proposed completion metadata that
the authoritative kernel passes to it.
"""

from __future__ import annotations

import json
import re
import time
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
_REQUIRED_DELIVERY_CONTROLS = (
    "ONE_BRANCH_ONE_WRITER",
    "PRODUCT_PLATFORM_PR_SEPARATION",
    "HEAD_FROZEN",
)
_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?$")
WORKFLOW_TEMPLATE = "anveros-delivery-v2"
_STATES = (
    "TRIAGE", "READY", "IMPLEMENTING", "PREVIEW_READY", "PRODUCT_REVIEW",
    "HEAD_FROZEN", "MERGE_QUEUED", "MERGED", "DEPLOYING",
    "PRODUCTION_VERIFY", "DONE", "ROLLBACK",
)
_NEXT = {
    "TRIAGE": {"READY", "ROLLBACK"},
    "READY": {"IMPLEMENTING", "ROLLBACK"},
    "IMPLEMENTING": {"PREVIEW_READY", "ROLLBACK"},
    "PREVIEW_READY": {"PRODUCT_REVIEW", "ROLLBACK"},
    "PRODUCT_REVIEW": {"HEAD_FROZEN", "ROLLBACK"},
    "HEAD_FROZEN": {"MERGE_QUEUED", "ROLLBACK"},
    "MERGE_QUEUED": {"MERGED", "ROLLBACK"},
    "MERGED": {"DEPLOYING", "ROLLBACK"},
    "DEPLOYING": {"PRODUCTION_VERIFY", "ROLLBACK"},
    "PRODUCTION_VERIFY": {"DONE", "ROLLBACK"},
    "DONE": set(),
    "ROLLBACK": set(),
}
_OWNER_ROLE = {
    "TRIAGE": "IMPLEMENTER",
    "READY": "IMPLEMENTER",
    "IMPLEMENTING": "IMPLEMENTER",
    "PREVIEW_READY": "IMPLEMENTER",
    "PRODUCT_REVIEW": "PRODUCT_VERIFIER",
    "HEAD_FROZEN": "PRODUCT_VERIFIER",
    "MERGE_QUEUED": "RELEASE_OWNER",
    "MERGED": "RELEASE_OWNER",
    "DEPLOYING": "RELEASE_OWNER",
    "PRODUCTION_VERIFY": "PRODUCTION_VERIFIER",
}


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


def _workflow_state(task: Any, contract: Mapping[str, Any]) -> str:
    state = getattr(task, "current_step_key", None) or contract.get("state") or "TRIAGE"
    return str(state).strip().upper()


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _delivery_control_issues(
    metadata: Any, contract: Mapping[str, Any],
) -> list[str]:
    """Validate raw branch/preview facts instead of trusting PASS labels."""
    controls = metadata.get("delivery_controls") if isinstance(metadata, Mapping) else None
    if not isinstance(controls, Mapping):
        return ["DELIVERY_CONTROLS_REQUIRED"]

    issues: list[str] = []
    writers = controls.get("writer_ids")
    writer_ids = {
        _identity(value) for value in writers
        if _identity(value)
    } if isinstance(writers, list) else set()
    if len(writer_ids) != 1:
        issues.append("ONE_BRANCH_ONE_WRITER_FAILED")

    product_files = controls.get("product_files")
    platform_files = controls.get("platform_files")
    product_files = product_files if isinstance(product_files, list) else []
    platform_files = platform_files if isinstance(platform_files, list) else []
    if not product_files:
        issues.append("PRODUCT_DELTA_FILES_REQUIRED")
    if product_files and platform_files:
        issues.append("PRODUCT_PLATFORM_PR_MIXED")

    preview_sha = controls.get("preview_head_sha")
    frozen_sha = controls.get("frozen_head_sha")
    reviewed_sha = controls.get("reviewed_head_sha")
    if any(not isinstance(value, str) or _SHA.fullmatch(value) is None
           for value in (preview_sha, frozen_sha, reviewed_sha)):
        issues.append("HEAD_FREEZE_SHA_INVALID")
    elif len({preview_sha, frozen_sha, reviewed_sha}) != 1:
        issues.append("HEAD_MOVED_AFTER_PREVIEW")

    corrections = _nonnegative_int(controls.get("post_freeze_correction_count"))
    if corrections is None or corrections > 1:
        issues.append("POST_FREEZE_CORRECTION_LIMIT_EXCEEDED")
    elif corrections == 1:
        authorized_by = _identity(controls.get("correction_authorized_by"))
        verifier = _role_profiles(contract).get("PRODUCT_VERIFIER", "")
        if not authorized_by or authorized_by != verifier:
            issues.append("POST_FREEZE_CORRECTION_NOT_VERIFIER_AUTHORIZED")

    thresholds = {
        "commit_count": (8, "STOP_SPLIT_COMMITS"),
        "changed_files": (8, "STOP_SPLIT_CHANGED_FILES"),
        "net_lines": (400, "STOP_SPLIT_NET_LINES"),
        "minutes_without_preview": (90, "STOP_SPLIT_PREVIEW_TIMEOUT"),
    }
    exceeded: list[str] = []
    for field, (maximum, reason) in thresholds.items():
        value = _nonnegative_int(controls.get(field))
        if value is None:
            issues.append(f"{field.upper()}_REQUIRED")
        elif value > maximum:
            exceeded.append(reason)
    if exceeded:
        checkpoint = controls.get("stop_split_checkpoint")
        checkpoint_verifier = _identity(controls.get("stop_split_verifier"))
        implementer = _identity(contract.get("implementer"))
        if checkpoint not in {"SPLIT", "BOUNDED_EXCEPTION"}:
            issues.extend(exceeded)
        if not checkpoint_verifier or checkpoint_verifier == implementer:
            issues.append("STOP_SPLIT_INDEPENDENT_DECISION_REQUIRED")
    return issues


def _role_profiles(contract: Mapping[str, Any]) -> dict[str, str]:
    configured = contract.get("role_profiles")
    configured = configured if isinstance(configured, Mapping) else {}
    defaults = {
        "IMPLEMENTER": contract.get("implementer"),
        "PRODUCT_VERIFIER": contract.get("product_verifier") or "verifier",
        "PLATFORM_FIXER": contract.get("platform_fixer") or "ops",
        "RELEASE_OWNER": contract.get("release_owner") or "orchestrator",
        "PRODUCTION_VERIFIER": contract.get("production_verifier") or "verifier",
    }
    return {
        role: _identity(configured.get(role) or defaults.get(role))
        for role in defaults
    }


def transition_delivery_state(args: Mapping[str, Any], **_: Any) -> str:
    """Advance one enrolled card through the existing native Kanban state.

    The transition is a bounded RPC: it persists ``current_step_key`` on the
    existing task row and hands the same task identity to the next native
    profile.  It neither creates cards nor starts a scheduler, broker or goal.
    """
    task_id = _identity(args.get("task_id"))
    actor = _identity(args.get("actor"))
    next_state = str(args.get("next_state") or "").strip().upper()
    if not task_id or not actor or next_state not in _STATES:
        return "Error: task_id, actor, and a valid next_state are required."
    from hermes_cli import kanban_db as kb

    conn = kb.connect()
    try:
        task = kb.get_task(conn, task_id)
        contract = _contract(task) if task is not None else None
        if contract is None:
            return "Error: task is not enrolled in delivery_v2."
        current = _workflow_state(task, contract)
        if current not in _STATES or next_state not in _NEXT.get(current, set()):
            return f"Error: invalid Delivery V2 transition {current} -> {next_state}."
        profiles = _role_profiles(contract)
        owner = profiles.get(_OWNER_ROLE.get(current, ""), "")
        if owner and actor != owner:
            return f"Error: {actor} is not the owner of {current}."
        if not kb.set_task_workflow_step(
            conn,
            task_id,
            workflow_template_id=WORKFLOW_TEMPLATE,
            current_step_key=next_state,
            expected_current_step_key=getattr(task, "current_step_key", None),
            reason=f"delivery_v2:{current}->{next_state}:actor={actor}",
        ):
            return "Error: concurrent Delivery V2 transition; reread the card."

        next_profile = profiles.get(_OWNER_ROLE.get(next_state, ""), "")
        if next_profile and next_profile != _identity(getattr(task, "assignee", None)):
            if next_state == "PRODUCT_REVIEW":
                kb.request_review(
                    conn,
                    task_id,
                    reviewer=next_profile,
                    expected_run_id=(
                        getattr(task, "current_run_id", None)
                        if getattr(task, "status", None) == "running"
                        else None
                    ),
                )
            else:
                kb.reassign_task(
                    conn,
                    task_id,
                    next_profile,
                    reclaim_first=getattr(task, "status", None) == "running",
                    reason=f"delivery_v2_handoff:{current}->{next_state}",
                )
        return f"Delivery V2 transition persisted: {current} -> {next_state}."
    finally:
        conn.close()


def on_kanban_dispatch_tick(*, board: str | None = None, **_: Any) -> None:
    """Use the existing Gateway tick to reroute an unclaimed enrolled card.

    Normal cards wait 30 minutes.  An explicitly sandbox-only card can lower
    the threshold for a bounded live canary.  The callback makes at most one
    reassignment: the fallback profile becomes the durable assignee.
    """
    if not board:
        return
    from hermes_cli import kanban_db as kb

    conn = kb.connect(board=board)
    try:
        now = int(time.time())
        for task in kb.list_tasks(conn, status="ready"):
            contract = _contract(task)
            if contract is None:
                continue
            fallback = contract.get("fallback_profiles")
            fallback = fallback if isinstance(fallback, Mapping) else {}
            current_assignee = _identity(getattr(task, "assignee", None))
            next_assignee = _identity(fallback.get(current_assignee))
            if not next_assignee or next_assignee == current_assignee:
                continue
            requested = contract.get("auto_reroute_after_seconds", 1800)
            try:
                threshold = int(requested)
            except (TypeError, ValueError):
                threshold = 1800
            minimum = 1 if contract.get("canary") == "SANDBOX_ONLY" else 1800
            threshold = max(minimum, threshold)
            if now - int(getattr(task, "created_at", now)) < threshold:
                continue
            kb.assign_task(conn, task.id, next_assignee)
    finally:
        conn.close()


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

    # Native Gateway dispatch claims a reviewer immediately.  The completion
    # boundary is then a verifier-owned ``running`` review run, rather than a
    # static ``review`` card.  Identity checks below remain mandatory.
    if getattr(task, "status", None) not in {"review", "running"}:
        issues.append("INDEPENDENT_REVIEW_STATE_REQUIRED")
    if _workflow_state(task, contract) != "PRODUCTION_VERIFY":
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
    for field in _REQUIRED_DELIVERY_CONTROLS:
        if receipt.get(field) != "PASS":
            issues.append(f"{field}_PASS_REQUIRED")
    issues.extend(_delivery_control_issues(metadata, contract))
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


def on_kanban_task_completed(*, task_id: str, board: str | None = None, **_: Any) -> None:
    """Close the enrolled workflow after the native completion transaction.

    ``before_kanban_task_complete`` is deliberately the sole synchronous
    boundary.  This observer runs only after native Kanban has durably marked
    the card done, then records the matching Delivery V2 terminal step on the
    same task row.  A failed observer cannot turn a completed card into a
    false success because the pre-completion receipt gate has already run.
    """
    if not task_id:
        return
    from hermes_cli import kanban_db as kb

    conn = kb.connect(board=board)
    try:
        task = kb.get_task(conn, task_id)
        contract = _contract(task) if task is not None else None
        if contract is None or _workflow_state(task, contract) != "PRODUCTION_VERIFY":
            return
        kb.set_task_workflow_step(
            conn,
            task_id,
            workflow_template_id=WORKFLOW_TEMPLATE,
            current_step_key="DONE",
            expected_current_step_key=getattr(task, "current_step_key", None),
            reason="delivery_v2:PRODUCTION_VERIFY->DONE:native_completion",
        )
    finally:
        conn.close()


def _decision(issues: list[str]) -> dict[str, Any]:
    unique = sorted(set(issues))
    if not unique:
        return {"allow": True}
    return {"allow": False, "reason": "delivery_v2:" + ",".join(unique)}


def register(ctx) -> None:
    """Register policy on existing Hermes hook and tool surfaces only."""
    ctx.register_hook("before_kanban_task_complete", before_kanban_task_complete)
    ctx.register_hook("on_kanban_dispatch_tick", on_kanban_dispatch_tick)
    ctx.register_hook("kanban_task_completed", on_kanban_task_completed)
    ctx.register_tool(
        name="delivery_v2_transition",
        toolset="kanban",
        schema={
            "name": "delivery_v2_transition",
            "description": "Persist one allowed Delivery V2 state transition and hand the same native Kanban card to its next role.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "actor": {"type": "string"},
                    "next_state": {"type": "string", "enum": list(_STATES)},
                },
                "required": ["task_id", "actor", "next_state"],
            },
        },
        handler=transition_delivery_state,
        description="Advance one Delivery V2 card through its durable native Kanban state.",
    )
