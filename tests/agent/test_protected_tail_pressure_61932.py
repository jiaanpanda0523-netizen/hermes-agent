"""Algorithmic reproduction and regression for issue #61932.

After several in-place compactions a tool-heavy session can be short enough
that nearly every remaining message sits inside the protected recent tail,
yet those messages are huge completed ``read_file`` / tool outputs.  The
middle compress window is then empty or tiny, preflight makes no material
token progress, and the turn dies with::

    Context length exceeded (174,833 tokens). Cannot compress further.

This is the core compressor contract — not Desktop/Windows-specific.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agent.context_compressor import (
    ContextCompressor,
    _MAX_TAIL_MESSAGE_FLOOR,
    _PRESSURE_KEEP_RECENT_MESSAGES,
)
from agent.model_metadata import estimate_messages_tokens_rough
from agent.turn_context import _compression_made_progress


def _unique_tool_pair(i: int, chars: int) -> list[dict]:
    """Assistant tool_call + unique tool result (no dedupe shortcut)."""
    body = f"FILE_{i}_START\n" + (f"line {i} unique payload " * (chars // 22))[:chars]
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"call_{i}",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": f'{{"path":"f{i}.py"}}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": f"call_{i}",
            "content": body,
        },
    ]


def _already_compacted_session(
    *,
    n_pairs: int,
    tool_chars: int,
    user_chars: int,
) -> list[dict]:
    """Shape after multiple in-place compactions: head + handoff + heavy tail."""
    msgs: list[dict] = [
        {"role": "system", "content": "You are Hermes."},
        {"role": "user", "content": "Investigate thoroughly"},
        {"role": "assistant", "content": "OK"},
        {
            "role": "user",
            "content": (
                "[CONTEXT COMPACTION — REFERENCE ONLY]\n"
                + ("Prior findings. " * 200)
            ),
        },
        {"role": "assistant", "content": "Continuing from compacted context."},
    ]
    for i in range(n_pairs):
        msgs.extend(_unique_tool_pair(i, tool_chars))
    msgs.append(
        {
            "role": "user",
            "content": "Full structured report:\n" + ("U" * user_chars),
        }
    )
    return msgs


@pytest.fixture()
def compressor_128k():
    with patch(
        "agent.context_compressor.get_model_context_length",
        return_value=128_000,
    ):
        c = ContextCompressor(
            model="openai-codex/gpt-test",
            threshold_percent=0.50,
            summary_target_ratio=0.20,
            protect_first_n=3,
            protect_last_n=20,
            quiet_mode=True,
            config_context_length=128_000,
        )
    c._generate_summary = lambda *a, **k: "compact summary of earlier investigation"
    return c


class TestProtectedTailPressure61932:

    def test_resumed_handoff_does_not_anchor_an_autonomous_tool_run_forever(
        self, compressor_128k
    ):
        """A resumed summary plus a long tool-only run must stay compactable.

        Production shape: the resumed handoff is the first active row, the
        latest real user ask is followed by hundreds of assistant(tool_calls)
        / tool(result) rows, and no later assistant prose bubble exists.  The
        user + visible-assistant tail anchors used to pull the cut back to
        index 1.  The handoff then consumed the entire one-row middle and every
        pass returned ``empty_post_handoff_window``; deterministic tool-body
        pruning helped once, but the remaining call metadata stayed above the
        compression threshold forever.
        """
        c = compressor_128k
        c.threshold_tokens_cap = 80_000
        c._apply_threshold_tokens_cap()
        c._generate_summary = lambda turns, **_kwargs: c._augment_summary_lean(
            c._with_summary_prefix("## Active Task\nSynthetic checkpoint."),
            turns,
        )
        active_ask = "Continue the investigation and finish it."
        msgs: list[dict] = [
            {
                "role": "user",
                "content": c._with_summary_prefix(
                    "## Active Task\nPrior investigation checkpoint."
                ),
            },
            {
                "role": "assistant",
                "content": "Visible reply before the autonomous tool run.",
            },
            {"role": "user", "content": active_ask},
        ]
        # Keep enough already-demotable tool metadata that the protected tail
        # remains above the 80K trigger even after Phase-1 body pruning.  A
        # smaller fixture falls below the trigger and is no longer a dead-end.
        for i in range(1_400):
            msgs.extend(_unique_tool_pair(i, 900))

        before = estimate_messages_tokens_rough(msgs)
        assert before > c.context_length

        out = c.compress(list(msgs), current_tokens=before, force=True)
        after = estimate_messages_tokens_rough(out)

        assert c._last_compression_made_progress is True
        assert (c._last_compression_telemetry or {}).get("failure_class") != (
            "empty_post_handoff_window"
        )
        assert after < c.threshold_tokens, (
            f"autonomous tool tail stayed armed for another pass: "
            f"{before:,} -> {after:,} >= {c.threshold_tokens:,}"
        )
        assert any(
            active_ask in str(message.get("content") or "") for message in out
        ), "the latest real user ask must survive verbatim in the lean summary"
        assert any(
            message.get("role") == "user"
            and message.get("content") == active_ask
            and not message.get("_compressed_summary")
            for message in out
        ), "the active ask must remain an ordinary recency-anchor row"
        assert any(
            message.get("role") == "assistant"
            and message.get("content")
            == "Visible reply before the autonomous tool run."
            and not message.get("_compressed_summary")
            for message in out
        ), "the last visible assistant reply must remain an ordinary row"

    def test_schema_pressure_activates_lean_tool_tail_escape(self, compressor_128k):
        """Full-request pressure includes schemas/system prompt, not messages only."""
        c = compressor_128k
        c.threshold_tokens_cap = 80_000
        c._apply_threshold_tokens_cap()
        c._generate_summary = lambda turns, **_kwargs: c._augment_summary_lean(
            c._with_summary_prefix("## Active Task\nSynthetic checkpoint."),
            turns,
        )
        msgs: list[dict] = [
            {
                "role": "user",
                "content": c._with_summary_prefix(
                    "## Active Task\nPrior investigation checkpoint."
                ),
            },
            {"role": "assistant", "content": "Visible reply."},
            {"role": "user", "content": "Keep investigating."},
        ]
        for i in range(1_100):
            msgs.extend(_unique_tool_pair(i, 900))

        before = estimate_messages_tokens_rough(msgs)
        out = c.compress(
            list(msgs),
            # 20K represents system prompt + tool schemas.  Phase-1 pruning
            # leaves the anchored message tail just below 80K; request-wide
            # pressure must still activate the escape.
            current_tokens=before + 20_000,
            force=True,
        )
        after = estimate_messages_tokens_rough(out)

        assert c._last_compression_made_progress is True
        assert after < c.threshold_tokens
        assert any(
            m.get("role") == "user" and m.get("content") == "Keep investigating."
            for m in out
        )

    def test_opt_in_n_user_anchors_survive_tool_tail_escape(self, compressor_128k):
        c = compressor_128k
        c.threshold_tokens_cap = 80_000
        c._apply_threshold_tokens_cap()
        c.min_tail_user_messages = 3
        c._generate_summary = lambda turns, **_kwargs: c._augment_summary_lean(
            c._with_summary_prefix("## Active Task\nSynthetic checkpoint."),
            turns,
        )
        asks = ["First retained ask.", "Second retained ask.", "Latest retained ask."]
        replies = ["First visible reply.", "Second visible reply."]
        msgs: list[dict] = [
            {
                "role": "user",
                "content": c._with_summary_prefix(
                    "## Active Task\nPrior investigation checkpoint."
                ),
            },
            {"role": "assistant", "content": "Pre-anchor reply."},
        ]
        for ask, reply in zip(asks[:2], replies):
            msgs.extend(
                [
                    {"role": "user", "content": ask},
                    {"role": "assistant", "content": reply},
                ]
            )
        msgs.append({"role": "user", "content": asks[-1]})
        for i in range(1_400):
            msgs.extend(_unique_tool_pair(i, 900))

        before = estimate_messages_tokens_rough(msgs)
        out = c.compress(list(msgs), current_tokens=before, force=True)

        ordinary_users = {
            m.get("content")
            for m in out
            if m.get("role") == "user"
            and not m.get("_compressed_summary")
        }
        ordinary_assistants = {
            m.get("content")
            for m in out
            if m.get("role") == "assistant"
            and not m.get("_compressed_summary")
        }
        assert set(asks) <= ordinary_users
        assert set(replies) <= ordinary_assistants
        assert estimate_messages_tokens_rough(out) < c.threshold_tokens

    def test_tool_call_prose_anchor_is_normalized_before_role_selection(
        self, compressor_128k
    ):
        """A prose assistant anchor must not change visibility after assembly.

        The assistant originally owns a completed tool call.  Lean recovery
        reinserts its visible prose but summarizes the paired tool result.  If
        role selection runs before the orphaned call is stripped, the strict
        template sees a different role sequence after sanitization.
        """
        c = compressor_128k
        c.threshold_tokens_cap = 80_000
        c._apply_threshold_tokens_cap()
        c._generate_summary = lambda turns, **_kwargs: c._augment_summary_lean(
            c._with_summary_prefix("## Active Task\nSynthetic checkpoint."),
            turns,
        )
        msgs: list[dict] = [
            {
                "role": "user",
                "content": c._with_summary_prefix(
                    "## Active Task\nPrior investigation checkpoint."
                ),
            },
            {
                "role": "assistant",
                "content": "Visible reply that also launched a tool.",
                "tool_calls": [
                    {
                        "id": "anchor_call",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
                "api_content": "stale replay bytes",
            },
            {
                "role": "tool",
                "tool_call_id": "anchor_call",
                "content": "completed anchor result",
            },
            {"role": "user", "content": "Keep investigating."},
        ]
        for i in range(1_400):
            msgs.extend(_unique_tool_pair(i, 900))

        before = estimate_messages_tokens_rough(msgs)
        out = c.compress(list(msgs), current_tokens=before, force=True)

        visible = [
            m.get("role")
            for m in out
            if m.get("role") != "tool"
            and not (m.get("role") == "assistant" and m.get("tool_calls"))
        ]
        assert visible
        assert visible[0] == "user"
        assert all(left != right for left, right in zip(visible, visible[1:]))
        anchor = next(
            m
            for m in out
            if m.get("content") == "Visible reply that also launched a tool."
        )
        assert anchor.get("role") == "assistant"
        assert "tool_calls" not in anchor
        assert "api_content" not in anchor



    def test_compress_escapes_cannot_compress_further_dead_end(
        self, compressor_128k
    ):
        """Full compress path must materially reduce an over-context tail.

        Reproduces the #61932 failure class: multipass compression previously
        dropped a couple of message rows while leaving ~170k tokens intact,
        then reported no further progress.
        """
        c = compressor_128k
        msgs = _already_compacted_session(
            n_pairs=4, tool_chars=200_000, user_chars=80_000
        )
        rough0 = estimate_messages_tokens_rough(msgs)
        assert rough0 > c.context_length

        cur = msgs
        tok = rough0
        last_progress = False
        for _pass in range(3):
            o_len, o_tok = len(cur), tok
            out = c.compress(list(cur), current_tokens=tok)
            n_tok = estimate_messages_tokens_rough(out)
            last_progress = _compression_made_progress(
                o_len, len(out), o_tok, n_tok
            )
            cur, tok = out, n_tok
            if n_tok < c.threshold_tokens and n_tok < c.context_length:
                break

        assert tok < c.context_length, (
            f"still over context after compression: {tok:,} >= {c.context_length:,}"
        )
        assert tok < rough0 * 0.5, (
            f"compression did not reclaim enough headroom: {rough0:,} → {tok:,}"
        )
        # Either we recovered under threshold, or the last pass still made
        # progress (never a pure no-op dead-end above the window).
        assert tok < c.threshold_tokens or last_progress

    def test_all_oversized_tail_dead_end_shape_now_compresses(
        self, compressor_128k
    ):
        """Exact #61932 dead-end: the protected tail ALONE holds everything.

        Head (3 messages) + an 8-message tail of exclusively oversized tool
        pairs.  The tail token budget + the ``_MAX_TAIL_MESSAGE_FLOOR`` (8)
        floor protect every non-head message, so ``compress_start >=
        compress_end`` — pre-fix ``compress()`` returned the transcript
        UNCHANGED, incremented ``_ineffective_compression_count``, and the
        retry loop died with "Cannot compress further".  Post-fix the Phase-1
        pressure pass demotes the oversized tool bodies even though the
        summary window is empty, so the same call materially shrinks the
        transcript below the context window.
        """
        c = compressor_128k
        msgs: list[dict] = [
            {"role": "system", "content": "You are Hermes."},
            {"role": "user", "content": "Investigate thoroughly"},
            {"role": "assistant", "content": "OK"},
        ]
        for i in range(4):
            msgs.extend(_unique_tool_pair(i, 200_000))
        assert len(msgs) == 11  # 3 head + 8-message all-oversized tail

        before = estimate_messages_tokens_rough(msgs)
        assert before > c.context_length, "fixture must start over-context"

        out = c.compress(list(msgs), current_tokens=before)
        after = estimate_messages_tokens_rough(out)

        # The dead-end is broken: one pass reclaims the bulk of the tail.
        assert after < c.context_length, (
            f"still over context: {after:,} >= {c.context_length:,}"
        )
        assert after < before * 0.25, (
            f"expected the oversized tail to demote: {before:,} → {after:,}"
        )

        # tool_call/tool_result pairing must survive demotion — never orphan
        # a tool result or a tool call (provider 400s otherwise).  Whole
        # pairs may legitimately be summarized away together.
        call_ids = {
            tc["id"]
            for m in out
            if m.get("role") == "assistant"
            for tc in (m.get("tool_calls") or [])
            if isinstance(tc, dict)
        }
        tool_result_ids = [
            m.get("tool_call_id") for m in out if m.get("role") == "tool"
        ]
        assert tool_result_ids, "expected surviving tool pairs in the tail"
        for rid in tool_result_ids:
            assert rid in call_ids, f"orphaned tool result {rid!r}"
        for cid in call_ids:
            assert cid in tool_result_ids, f"orphaned tool call {cid!r}"
