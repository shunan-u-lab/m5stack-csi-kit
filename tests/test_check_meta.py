"""scripts/check_meta.py のテスト."""

from __future__ import annotations

import importlib.util
import sys
import zlib
from pathlib import Path
from types import ModuleType

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_meta.py"
RAW_HEX = "00" * 256


def load_script() -> ModuleType:
    """scripts/ はパッケージではないので、ファイルから読み込む."""
    spec = importlib.util.spec_from_file_location("check_meta", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_meta"] = module
    spec.loader.exec_module(module)
    return module


check = load_script()


def write_csv(path: Path) -> None:
    """最小の記録（CSI 5 行）を書く."""
    lines = ["# capture start=2026-09-23T15:00:00 port=x"]
    for i in range(5):
        body = f"CSI,{i * 10_000},{i},-60,-96,11,0,1,0,0,0,0,0,256,{RAW_HEX}"
        lines.append(f"{body}*{zlib.crc32(body.encode()):08x}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_exit_codes_follow_missing_draft_and_todo(tmp_path: Path) -> None:
    """欠けは 1、--draft で下書きを書くと未記入が残るので 1、--allow-todo なら 0."""
    raw, meta_dir = tmp_path / "raw", tmp_path / "meta"
    raw.mkdir()
    meta_dir.mkdir()
    write_csv(raw / "20260923-150000-walk.csv")
    common = ["--raw-dir", str(raw), "--meta-dir", str(meta_dir)]
    assert check.main(common) == 1
    assert check.main([*common, "--draft"]) == 1
    assert (meta_dir / "20260923-150000-walk.toml").exists()
    assert check.main([*common, "--allow-todo"]) == 0


def test_missing_directory_is_an_error(tmp_path: Path) -> None:
    """ディレクトリがなければ 1."""
    assert check.main(["--raw-dir", str(tmp_path / "none"), "--meta-dir", str(tmp_path)]) == 1
