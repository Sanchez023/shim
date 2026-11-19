from __future__ import annotations

from pathlib import Path

from openharness.ui.file_search import list_project_files, rank_files, search_files


def test_search_files_fuzzy_matches_paths(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.tsx").write_text("export {}")
    (tmp_path / "src" / "backend.py").write_text("print('x')")
    (tmp_path / "README.md").write_text("# readme")

    assert search_files(tmp_path, "app")[0] == "src/app.tsx"
    assert "README.md" in search_files(tmp_path, "read")
    assert search_files(tmp_path, "zzz") == []


def test_list_project_files_skips_vendor_directories(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("x")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "pkg.js").write_text("x")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("x")

    files = list_project_files(tmp_path)

    assert files == ["src/main.py"]


def test_rank_files_prefers_contiguous_matches():
    paths = ["src/application/components.py", "src/app.py", "docs/appendix.md"]

    ranked = rank_files(paths, "app")

    assert ranked[0] == "src/app.py"
