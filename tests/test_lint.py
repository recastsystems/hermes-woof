import importlib.util
import subprocess
import sys

from conftest import ROOT


def _lint_module():
    spec = importlib.util.spec_from_file_location("lint_text", ROOT / "scripts" / "lint_text.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_shipped_text_is_clean():
    p = subprocess.run([sys.executable, str(ROOT / "scripts" / "lint_text.py"), str(ROOT)], capture_output=True, text=True)
    assert p.returncode == 0, p.stdout[-3000:]


def test_lint_catches_each_pattern_rule(tmp_path):
    bad = tmp_path / "bad.md"
    lines = ["em " + chr(0x2014) + " dash", "hon" + "est", "load-" + "bearing", "9" + "5 percent",
             "/ho" + "me/someone", "--hea" + "vy", "e" + "cape", "192.16" + "8.0.7"]
    bad.write_text("\n".join(lines) + "\n", encoding="utf-8")
    p = subprocess.run([sys.executable, str(ROOT / "scripts" / "lint_text.py"), str(bad)], capture_output=True, text=True)
    assert p.returncode == 1
    assert p.stdout.count("bad.md:") == len(lines)


def test_hashed_words_are_caught_alone_in_pairs_and_split(monkeypatch):
    """The kept-out words are stored as hashes; check the matching with stand-in words."""
    lint = _lint_module()
    monkeypatch.setitem(lint.HASHED, "person name", {lint.word_hash("quillonbray")})
    monkeypatch.setitem(lint.HASHED, "internal machine", {lint.word_hash("zephyrbox7")})
    assert lint.hashed_hits("a note from Quillon Bray") == ["person name"]
    assert lint.hashed_hits("QUILLONBRAY said") == ["person name"]
    assert lint.hashed_hits("ran on zephyrbox-7 overnight") == ["internal machine"]
    assert lint.hashed_hits("ran on zephyrbox_7") == ["internal machine"]
    assert lint.hashed_hits("quillon and bray, zephyrbox 8") == []


def test_every_kept_out_class_has_entries():
    lint = _lint_module()
    for label in ("person name", "internal machine", "other product name", "former engine name"):
        assert lint.HASHED[label] and all(len(h) == 16 for h in lint.HASHED[label])
