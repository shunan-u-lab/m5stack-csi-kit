"""scripts/fill_meta.py のテスト."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from csikit import meta

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "fill_meta.py"
EXAMPLE = ROOT / "examples" / "meta" / "20260101-120000-static.toml"


def load_script() -> ModuleType:
    """scripts/ はパッケージではないので、ファイルから読み込む."""
    spec = importlib.util.spec_from_file_location("fill_meta", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["fill_meta"] = module
    spec.loader.exec_module(module)
    return module


fill = load_script()


def make_draft(tmp_path: Path, name: str) -> Path:
    """TODO を含む下書きを作る（記入例を元に、いくつかを TODO に戻す）."""
    data = meta.load_meta(EXAMPLE)
    data["recording"] = name
    data["purpose"] = meta.TODO
    data["segments"] = [
        {"start_sec": 0.0, "end_sec": 10.0, "activity": meta.TODO, "where": meta.TODO},
        {"start_sec": 10.0, "end_sec": 20.0, "activity": "walk", "where": meta.TODO}]
    data["status"]["usable"] = meta.TODO
    path = tmp_path / f"{name}.toml"
    path.write_text(meta.dumps_meta(data, ["元の注記"]), encoding="utf-8")
    return path


def test_fills_only_todo_across_files(tmp_path: Path) -> None:
    """全ファイルの TODO を埋め、記入済みの値は残し、先頭の注記も残す."""
    files = [make_draft(tmp_path, f"20260930-10000{i}-exp05b-s1-ch0{i}") for i in (1, 2)]
    argv = [*map(str, files), "--set", 'purpose="掃引"', "--set", 'segments[*].activity="static"',
            "--set", 'segments[*].where="席"', "--set", "status.usable=true"]
    assert fill.main(argv) == 0
    for f in files:
        data = meta.load_meta(f)
        assert data["purpose"] == "掃引" and data["status"]["usable"] is True
        assert [s["activity"] for s in data["segments"]] == ["static", "walk"]  # walk は残る
        assert [s["where"] for s in data["segments"]] == ["席", "席"]
        assert meta.find_todos(data) == []
        text = f.read_text(encoding="utf-8")
        assert text.startswith("# 元の注記\n") and "fill_meta.py で一括記入" in text


def test_force_overwrites_and_dry_run_does_not_write(tmp_path: Path) -> None:
    """--force は記入済みも上書きし、--dry-run はファイルを変えない."""
    f = make_draft(tmp_path, "20260930-100001-exp05b-s1-ch01")
    before = f.read_text(encoding="utf-8")
    assert fill.main([str(f), "--set", 'segments[1].activity="static"', "--dry-run",
                      "--force"]) == 0
    assert f.read_text(encoding="utf-8") == before
    assert fill.main([str(f), "--set", 'segments[1].activity="static"', "--force"]) == 0
    assert meta.load_meta(f)["segments"][1]["activity"] == "static"


def test_refuses_values_that_break_the_format(tmp_path: Path) -> None:
    """形の問題が出る値（usable に文字列）は書き込まず、終了コード 1."""
    f = make_draft(tmp_path, "20260930-100001-exp05b-s1-ch01")
    before = f.read_text(encoding="utf-8")
    assert fill.main([str(f), "--set", 'status.usable="yes"']) == 1
    assert f.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("assignment", ["purpose", "a..b=1", 'purpose=掃引', "x[1=2"])
def test_rejects_malformed_assignments(tmp_path: Path, assignment: str) -> None:
    """キー=値 の形・キー・値（引用符なしの文字列）の誤りを弾く."""
    f = make_draft(tmp_path, "20260930-100001-exp05b-s1-ch01")
    assert fill.main([str(f), "--set", assignment]) == 1


def test_missing_key_path_is_an_error(tmp_path: Path) -> None:
    """たどる途中の表がなければ、そのファイルは書かずに終了コード 1."""
    f = make_draft(tmp_path, "20260930-100001-exp05b-s1-ch01")
    assert fill.main([str(f), "--set", "nothing.here=1"]) == 1
