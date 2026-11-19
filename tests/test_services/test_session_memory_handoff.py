"""The session-memory hand-off must state verification, not imply it."""

from __future__ import annotations

from pathlib import Path

from openharness.domain.messages import ConversationMessage
from openharness.engine import verification_ledger as ledger
from openharness.services.session_memory import build_session_memory_document


def document(tmp_path: Path, metadata: dict[str, object]) -> str:
    messages = [ConversationMessage.from_user_text("refactor the loop")]
    return build_session_memory_document(messages, tool_metadata=metadata)


def test_no_ledger_means_no_verification_claim(tmp_path: Path) -> None:
    text = document(tmp_path, {})

    assert "## Verification" not in text
    assert "## Explored" not in text


def test_unverified_edits_are_stated_out_loud(tmp_path: Path) -> None:
    metadata: dict[str, object] = {}
    ledger.record_edit(metadata, "src/openharness/engine/query.py")
    ledger.record_run(metadata, command="pytest -q", returncode=0)
    ledger.record_edit(metadata, "src/openharness/engine/step_runner.py")

    text = document(tmp_path, metadata)

    assert "## Verification" in text
    assert "last green full run: `pytest -q`" in text
    assert "UNVERIFIED" in text
    assert "1 edit(s) since the last green run" in text


def test_a_closed_gate_says_so_without_bragging(tmp_path: Path) -> None:
    metadata: dict[str, object] = {}
    ledger.record_edit(metadata, "src/openharness/engine/query.py")
    ledger.record_run(metadata, command="pytest -q", returncode=0)

    text = document(tmp_path, metadata)

    assert "no edits since that run" in text
    assert "UNVERIFIED" not in text


def test_exploration_is_filed_under_explored(tmp_path: Path) -> None:
    metadata: dict[str, object] = {
        "task_focus_state": {
            "goal": "land the gate",
            "recent_goals": [],
            "active_artifacts": [],
            "verified_state": ["Inspected file src/openharness/engine/query.py (lines 1-40)"],
            "next_step": "",
        }
    }

    text = document(tmp_path, metadata)

    assert "## Explored" in text
    assert "## Verified Work" not in text
    assert "Inspected file" in text


def test_open_checklist_items_survive_the_hand_off(tmp_path: Path) -> None:
    plan = tmp_path / "plan.md"
    plan.write_text(
        "- [x] Phase 1\n- [ ] Phase 2: wire the gate\n- [ ] Phase 3: full run\n",
        encoding="utf-8",
    )

    text = document(tmp_path, {"plan_file": str(plan)})

    assert "## Open Items" in text
    assert "- Phase 2: wire the gate" in text
    assert "- Phase 3: full run" in text
    assert "Phase 1" not in text
