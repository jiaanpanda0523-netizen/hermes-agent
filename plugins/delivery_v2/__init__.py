"""Delivery OS V2 policy on Hermes' typed Kanban completion boundary.

This is intentionally a small policy plugin, not a task store, dispatcher or
command parser. Hermes Kanban remains the only durable task authority; the
plugin only evaluates the task snapshot and proposed completion metadata that
the authoritative kernel passes to it.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse


PRODUCTION_CHANGE = "PRODUCTION_CHANGE"
NON_PRODUCTION_DELIVERABLE = "NON_PRODUCTION_DELIVERABLE"
_TASK_CLASSES = {PRODUCTION_CHANGE, NON_PRODUCTION_DELIVERABLE}
_REQUIRED_DELIVERY_CONTROLS = (
    "ONE_BRANCH_ONE_WRITER",
    "PRODUCT_PLATFORM_PR_SEPARATION",
    "HEAD_FROZEN",
)
_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_SHA256 = re.compile(r"[0-9a-f]{64}$")
_PRODUCTION_EVIDENCE_KINDS = {
    "merge", "deployment", "acceptance", "user_visible_delta", "rollback",
}
WORKFLOW_TEMPLATE = "anveros-delivery-v2"
_STRICT_AFTER_EPOCH: int | None = None
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


def _url_is_production_https(value: Any) -> bool:
    if not _url_is_https(value):
        return False
    host = (urlparse(value).hostname or "").casefold()
    return bool(host) and host not in {"localhost", "127.0.0.1", "::1"} and not host.endswith(".invalid")


def _authoritative_evidence_source(
    kind: str,
    source_url: Any,
    production_url: Any,
    exact_sha: Any,
    contract: Mapping[str, Any],
) -> bool:
    if not _url_is_production_https(source_url):
        return False
    source = urlparse(source_url)
    repository = str(contract.get("repository") or "").strip().casefold()
    if kind == "merge":
        pull_request = contract.get("pull_request_number")
        return (
            (source.hostname or "").casefold() == "api.github.com"
            and isinstance(pull_request, int)
            and not isinstance(pull_request, bool)
            and source.path.rstrip("/").casefold()
            == f"/repos/{repository}/pulls/{pull_request}"
        )
    if kind == "rollback":
        return (
            (source.hostname or "").casefold() == "api.github.com"
            and source.path.rstrip("/").casefold()
            == f"/repos/{repository}/commits/{str(exact_sha).casefold()}"
        )
    production = urlparse(str(production_url or ""))
    enrolled = urlparse(str(contract.get("production_origin") or ""))
    return (
        (source.hostname or "").casefold()
        == (production.hostname or "").casefold()
        == (enrolled.hostname or "").casefold()
        and source.scheme == production.scheme == enrolled.scheme == "https"
        and source.port == production.port == enrolled.port
    )


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
    worker_task_id = _identity(os.environ.get("HERMES_KANBAN_TASK"))
    next_state = str(args.get("next_state") or "").strip().upper()
    if not task_id or next_state not in _STATES:
        return "Error: task_id and a valid next_state are required."
    if (
        worker_task_id != task_id
    ):
        return "Error: trusted current Hermes task provenance does not match target task."
    from hermes_cli import kanban_db as kb
    try:
        from hermes_cli.profiles import get_active_profile_name

        active_profile = _identity(
            os.environ.get("HERMES_PROFILE") or get_active_profile_name()
        )
    except Exception:
        active_profile = _identity(os.environ.get("HERMES_PROFILE"))

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
        run_id = getattr(task, "current_run_id", None)
        try:
            worker_run_id = int(os.environ.get("HERMES_KANBAN_RUN_ID", ""))
        except ValueError:
            worker_run_id = None
        if (
            not active_profile
            or getattr(task, "status", None) != "running"
            or run_id is None
            or worker_run_id != run_id
            or _identity(getattr(task, "assignee", None)) != active_profile
            or (owner and active_profile != owner)
        ):
            return "Error: trusted active run provenance does not own this transition."
        run = conn.execute(
            "SELECT profile, status, claim_lock, ended_at FROM task_runs "
            "WHERE id = ? AND task_id = ?",
            (int(run_id), task_id),
        ).fetchone()
        if (
            run is None
            or _identity(run["profile"]) != active_profile
            or run["status"] != "running"
            or run["ended_at"] is not None
            or run["claim_lock"] != getattr(task, "claim_lock", None)
        ):
            return "Error: trusted active run provenance does not own this transition."
        next_profile = profiles.get(_OWNER_ROLE.get(next_state, ""), "")
        handoff = None
        if next_profile and next_profile != active_profile:
            handoff = "review" if next_state == "PRODUCT_REVIEW" else "reassign"
        reason = f"delivery_v2:{current}->{next_state}:run={int(run_id)}"
        try:
            persisted = kb.transition_workflow_task(
                conn,
                task_id,
                workflow_template_id=WORKFLOW_TEMPLATE,
                expected_current_step_key=getattr(task, "current_step_key", None),
                current_step_key=next_state,
                expected_assignee=active_profile,
                expected_run_id=int(run_id),
                next_assignee=next_profile if handoff else None,
                handoff=handoff,
                reason=reason,
            )
        except Exception as exc:
            return f"Error: atomic Delivery V2 transition failed ({type(exc).__name__})."
        if not persisted:
            return "Error: concurrent or stale Delivery V2 transition; reread the card."
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
            kb.reroute_ready_task_once(
                conn,
                task.id,
                expected_assignee=current_assignee,
                next_assignee=next_assignee,
                threshold_seconds=threshold,
                now=now,
            )
    finally:
        conn.close()


def on_kanban_task_blocked(
    *, task_id: str, board: str | None = None, assignee: str | None = None,
    **_: Any,
) -> None:
    """Escalate a verifier BLOCK to the existing platform-fixer profile.

    The native block transition remains authoritative and local to this card.
    This observer only changes its durable assignee, preventing dependency
    promotion from sending an unchanged review straight back to the verifier.
    """
    if not task_id:
        return
    from hermes_cli import kanban_db as kb

    conn = kb.connect(board=board)
    try:
        task = kb.get_task(conn, task_id)
        contract = _contract(task) if task is not None else None
        if contract is None:
            return
        state = _workflow_state(task, contract)
        if state not in {"PRODUCT_REVIEW", "HEAD_FROZEN", "PRODUCTION_VERIFY"}:
            return
        profiles = _role_profiles(contract)
        verifier_profiles = {
            profiles.get("PRODUCT_VERIFIER", ""),
            profiles.get("PRODUCTION_VERIFIER", ""),
        }
        blocked_by = _identity(assignee or getattr(task, "assignee", None))
        fixer = profiles.get("PLATFORM_FIXER", "")
        if not fixer or blocked_by not in verifier_profiles or fixer == blocked_by:
            return
        kb.reassign_task(
            conn,
            task_id,
            fixer,
            reclaim_first=getattr(task, "status", None) == "running",
            reason=f"delivery_v2_verifier_block:{state}",
        )
        if getattr(task, "block_kind", None) == "transient":
            kb.unblock_task(conn, task_id)
    finally:
        conn.close()


def before_kanban_task_complete(
    *, task: Any, actor: str, metadata: Any,
    run_provenance: Any = None, evidence_provenance: Any = None, **_: Any,
) -> dict[str, Any] | None:
    """Grandfather pre-cutover cards; fail closed for every newer task."""
    contract = _contract(task)
    if contract is None:
        created_at = getattr(task, "created_at", None)
        if (
            _STRICT_AFTER_EPOCH is not None
            and isinstance(created_at, int)
            and created_at >= _STRICT_AFTER_EPOCH
        ):
            return _decision(["TASK_CLASSIFICATION_REQUIRED"])
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
    if getattr(task, "current_step_key", None) != "PRODUCTION_VERIFY":
        issues.append("PRODUCTION_VERIFY_STATE_REQUIRED")
    evidence = metadata.get("production_evidence") if isinstance(metadata, Mapping) else None
    if not isinstance(evidence, Mapping):
        issues.append("STRUCTURED_PRODUCTION_EVIDENCE_REQUIRED")
        evidence = {}
    if evidence.get("scope") != "PRODUCTION":
        issues.append("PRODUCTION_EVIDENCE_SCOPE_REQUIRED")
    merged_sha = evidence.get("merged_sha")
    deployed_sha = evidence.get("deployed_sha")
    for field, value in (("MERGED_SHA", merged_sha), ("DEPLOYED_SHA", deployed_sha)):
        if not isinstance(value, str) or _SHA.fullmatch(value) is None:
            issues.append(f"{field}_INVALID")
    if merged_sha != deployed_sha:
        issues.append("EXACT_SHA_MATCH_FAILED")
    if not _url_is_production_https(evidence.get("production_url")):
        issues.append("PRODUCTION_URL_INVALID")
    production_origin = str(contract.get("production_origin") or "").rstrip("/")
    production_url = str(evidence.get("production_url") or "")
    parsed_production = urlparse(production_url)
    parsed_origin = urlparse(production_origin)
    if (
        not _url_is_production_https(production_origin)
        or parsed_production.scheme != parsed_origin.scheme
        or parsed_production.hostname != parsed_origin.hostname
        or parsed_production.port != parsed_origin.port
    ):
        issues.append("PRODUCTION_ORIGIN_MISMATCH")
    repository = str(contract.get("repository") or "").strip().casefold()
    production_branch = str(contract.get("production_branch") or "").strip()
    pull_request_number = contract.get("pull_request_number")
    if (
        re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", repository) is None
        or not production_branch
        or not isinstance(pull_request_number, int)
        or isinstance(pull_request_number, bool)
        or pull_request_number < 1
    ):
        issues.append("DURABLE_RELEASE_TARGET_REQUIRED")
    if evidence.get("deployment_status") != "SUCCESS":
        issues.append("DEPLOYMENT_STATUS_SUCCESS_REQUIRED")
    if evidence.get("acceptance_status") != "PASS":
        issues.append("PRODUCTION_ACCEPTANCE_PROBE_PASS_REQUIRED")
    rollback_target = evidence.get("rollback_target_sha")
    if (
        not isinstance(rollback_target, str)
        or _SHA.fullmatch(rollback_target) is None
        or rollback_target == deployed_sha
    ):
        issues.append("ROLLBACK_TARGET_SHA_INVALID")
    for field in _REQUIRED_DELIVERY_CONTROLS:
        if evidence.get(field) != "PASS":
            issues.append(f"{field}_PASS_REQUIRED")
    issues.extend(_delivery_control_issues(metadata, contract))

    provenance = run_provenance if isinstance(run_provenance, Mapping) else {}
    implementer = _identity(contract.get("implementer"))
    verifier = _identity(provenance.get("run_profile"))
    expected_verifier = _role_profiles(contract).get("PRODUCTION_VERIFIER", "")
    now = int(time.time())
    prior_runs = {
        item.get("run_id"): item
        for item in provenance.get("prior_runs", [])
        if isinstance(item, Mapping) and isinstance(item.get("run_id"), int)
    } if isinstance(provenance.get("prior_runs"), list) else {}
    implementer_run = prior_runs.get(evidence.get("implementer_run_id"), {})
    if not implementer:
        issues.append("IMPLEMENTER_REQUIRED")
    if (
        not verifier
        or verifier != expected_verifier
        or verifier != _identity(provenance.get("process_profile"))
        or verifier != _identity(provenance.get("task_assignee"))
        or _identity(provenance.get("worker_task_id")) != _identity(getattr(task, "id", None))
        or provenance.get("worker_run_id") != provenance.get("run_id")
        or provenance.get("task_current_run_id") != provenance.get("run_id")
        or provenance.get("run_status") != "running"
        or provenance.get("run_ended_at") is not None
        or not provenance.get("task_claim_lock")
        or provenance.get("task_claim_lock") != provenance.get("run_claim_lock")
        or not isinstance(provenance.get("task_claim_expires"), int)
        or provenance.get("task_claim_expires") < now
        or not isinstance(provenance.get("run_claim_expires"), int)
        or provenance.get("run_claim_expires") < now
        or provenance.get("task_worker_pid") != provenance.get("process_pid")
        or provenance.get("run_worker_pid") != provenance.get("process_pid")
        or evidence.get("verifier_run_id") != provenance.get("run_id")
        or not _identity(provenance.get("worker_session_id"))
        or evidence.get("verifier_session_id") != provenance.get("worker_session_id")
        or _identity(implementer_run.get("profile")) != implementer
        or implementer_run.get("status") != "review"
        or implementer_run.get("outcome") != "review_requested"
        or implementer_run.get("claimed_event") is not True
        or implementer_run.get("review_requested_event") is not True
        or not isinstance(implementer_run.get("ended_at"), int)
        or not isinstance(provenance.get("run_started_at"), int)
        or implementer_run.get("ended_at") > provenance.get("run_started_at")
        or implementer == verifier
    ):
        issues.append("INDEPENDENT_PRODUCTION_VERIFIER_REQUIRED")

    resolved = {
        item.get("attachment_id"): item
        for item in evidence_provenance
        if isinstance(item, Mapping)
    } if isinstance(evidence_provenance, list) else {}
    refs = evidence.get("refs")
    seen_kinds: set[str] = set()
    if not isinstance(refs, list):
        issues.append("PRODUCTION_EVIDENCE_REFS_REQUIRED")
        refs = []
    for ref in refs:
        if not isinstance(ref, Mapping):
            issues.append("PRODUCTION_EVIDENCE_REF_MALFORMED")
            continue
        kind = str(ref.get("kind") or "").strip()
        attachment_id = ref.get("attachment_id")
        actual = resolved.get(attachment_id)
        source_ref = ref.get("source_ref")
        seen_kinds.add(kind)
        expected_sha = (
            merged_sha if kind == "merge"
            else rollback_target if kind == "rollback"
            else deployed_sha
        )
        if kind not in _PRODUCTION_EVIDENCE_KINDS:
            issues.append("PRODUCTION_EVIDENCE_KIND_INVALID")
        if ref.get("scope") != "PRODUCTION":
            issues.append("SANDBOX_EVIDENCE_FORBIDDEN")
        if ref.get("ref") != f"kanban-attachment:{attachment_id}":
            issues.append("PRODUCTION_EVIDENCE_REF_INVALID")
        if not isinstance(ref.get("sha256"), str) or _SHA256.fullmatch(ref["sha256"]) is None:
            issues.append("PRODUCTION_EVIDENCE_HASH_INVALID")
        if ref.get("exact_sha") != expected_sha:
            issues.append("PRODUCTION_EVIDENCE_EXACT_SHA_MISMATCH")
        if (
            actual is None
            or actual.get("sha256") != ref.get("sha256")
            or actual.get("recorded_size") != actual.get("size")
            or _identity(actual.get("uploaded_by")) != verifier
            or actual.get("run_id") != provenance.get("run_id")
            or actual.get("source_url") != source_ref
            or not _authoritative_evidence_source(
                kind,
                source_ref,
                evidence.get("production_url"),
                expected_sha,
                contract,
            )
            or not isinstance(actual.get("created_at"), int)
            or actual.get("created_at") < provenance.get("run_started_at", 0)
        ):
            issues.append("PRODUCTION_EVIDENCE_ATTACHMENT_UNVERIFIED")
            document = {}
        else:
            document = actual.get("document")
            document = document if isinstance(document, Mapping) else {}
        if kind == "merge" and (
            not _identity(document.get("merged_at"))
            or document.get("merge_commit_sha") != expected_sha
            or document.get("base", {}).get("ref") != production_branch
            or _identity(document.get("base", {}).get("repo", {}).get("full_name"))
            != repository
            or document.get("number") != pull_request_number
        ):
            issues.append("GITHUB_MERGE_EVIDENCE_INVALID")
        if kind == "rollback" and document.get("sha") != expected_sha:
            issues.append("GITHUB_ROLLBACK_COMMIT_EVIDENCE_INVALID")
        if kind == "deployment" and (
            document.get("deployed_sha") != expected_sha
            or document.get("deployment_status") != "SUCCESS"
            or document.get("production_url") != evidence.get("production_url")
        ):
            issues.append("DEPLOYMENT_EVIDENCE_INVALID")
        if kind == "acceptance" and (
            document.get("exact_sha") != expected_sha
            or document.get("acceptance_status") != "PASS"
            or document.get("production_url") != evidence.get("production_url")
        ):
            issues.append("ACCEPTANCE_EVIDENCE_INVALID")
        if kind == "user_visible_delta" and (
            document.get("exact_sha") != expected_sha
            or not _identity(document.get("evidence"))
        ):
            issues.append("USER_VISIBLE_DELTA_EVIDENCE_REQUIRED")
    if not _PRODUCTION_EVIDENCE_KINDS.issubset(seen_kinds):
        issues.append("PRODUCTION_EVIDENCE_KINDS_INCOMPLETE")
    decision = _decision(issues)
    if decision.get("allow") is True:
        decision["workflow_terminal_step"] = "DONE"
    return decision


def _decision(issues: list[str]) -> dict[str, Any]:
    unique = sorted(set(issues))
    if not unique:
        return {"allow": True}
    return {"allow": False, "reason": "delivery_v2:" + ",".join(unique)}


def register(ctx) -> None:
    """Register policy on existing Hermes hook and tool surfaces only."""
    global _STRICT_AFTER_EPOCH
    strict_after = ctx.get_config("strict_after_epoch")
    _STRICT_AFTER_EPOCH = (
        strict_after
        if isinstance(strict_after, int) and not isinstance(strict_after, bool)
        else None
    )
    ctx.register_hook("before_kanban_task_complete", before_kanban_task_complete)
    ctx.register_hook("on_kanban_dispatch_tick", on_kanban_dispatch_tick)
    ctx.register_hook("kanban_task_blocked", on_kanban_task_blocked)
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
                    "next_state": {"type": "string", "enum": list(_STATES)},
                },
                "required": ["task_id", "next_state"],
            },
        },
        handler=transition_delivery_state,
        description="Advance one Delivery V2 card through its durable native Kanban state.",
    )
