from __future__ import annotations

from pathlib import Path

from openharness.prompts.active_files import (
    collect_active_files,
    matches_globs,
    metadata_has_active_files,
    prompt_mentions_files,
)


def test_collect_active_files_from_prompt_and_metadata(tmp_path: Path):
    metadata = {
        "read_file_state": [{"path": "src/read.ts"}],
        "task_focus_state": {"active_artifacts": ["file: src/active.tsx"]},
        "recent_work_log": ["Edited src/log.py"],
    }

    files = collect_active_files("look at @src/main.ts and src/other.py", metadata)

    assert "src/main.ts" in files
    assert "src/other.py" in files
    assert "src/read.ts" in files
    assert "file: src/active.tsx" in files
    assert "src/active.tsx" in files
    assert "src/log.py" in files


def test_metadata_has_active_files():
    assert metadata_has_active_files({"read_file_state": [{"path": "a.py"}]})
    assert metadata_has_active_files(
        {"task_focus_state": {"active_artifacts": ["a.py"]}}
    )
    assert metadata_has_active_files({}) is False
    assert metadata_has_active_files(None) is False


def test_prompt_mentions_files_detects_mentions_and_paths():
    assert prompt_mentions_files("@src/app.ts")
    assert prompt_mentions_files("fix src/app.ts please")
    assert prompt_mentions_files("hello there") is False


def test_matches_globs_respects_path_segments(tmp_path: Path):
    assert matches_globs("src/app.ts", ["src/**/*.ts"], tmp_path)
    assert matches_globs("src/deep/app.ts", ["src/**/*.ts"], tmp_path)
    assert matches_globs("src/app.ts", ["*.ts"], tmp_path) is False
    assert matches_globs("app.ts", ["*.ts"], tmp_path)
    assert matches_globs("docs/readme.md", ["src/**/*.ts"], tmp_path) is False


def test_matches_globs_expands_braces(tmp_path: Path):
    assert matches_globs("src/app.tsx", ["src/**/*.{ts,tsx}"], tmp_path)
    assert matches_globs("src/app.ts", ["src/**/*.{ts,tsx}"], tmp_path)
    assert matches_globs("src/app.css", ["src/**/*.{ts,tsx}"], tmp_path) is False


def test_matches_globs_normalizes_absolute_paths(tmp_path: Path):
    absolute = str(tmp_path / "src" / "app.ts")

    assert matches_globs(absolute, ["src/**/*.ts"], tmp_path)
