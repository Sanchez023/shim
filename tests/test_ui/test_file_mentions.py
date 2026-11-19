from __future__ import annotations

from pathlib import Path

from openharness.ui.file_mentions import expand_file_mentions


def test_expand_file_mentions_includes_content(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")

    expanded, attached = expand_file_mentions("look at @src/app.py", tmp_path)

    assert attached == ["src/app.py"]
    assert '<file path="src/app.py">' in expanded
    assert "print('hi')" in expanded


def test_expand_file_mentions_skips_missing_outside_and_sensitive(tmp_path: Path):
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("outside", encoding="utf-8")
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "id_rsa").write_text("secret", encoding="utf-8")

    expanded, attached = expand_file_mentions(
        "read @missing.py @../outside-secret.txt @.ssh/id_rsa",
        tmp_path,
    )

    assert attached == []
    assert expanded == "read @missing.py @../outside-secret.txt @.ssh/id_rsa"
