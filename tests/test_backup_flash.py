"""scripts/backup_flash.py のテスト（esptool は偽物に差し替える）."""

from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
from datetime import date
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "backup_flash.py"


def load_script() -> ModuleType:
    """scripts/ はパッケージではないので、ファイルから読み込む."""
    spec = importlib.util.spec_from_file_location("backup_flash", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["backup_flash"] = module
    spec.loader.exec_module(module)
    return module


bf = load_script()

FLASH_MB = 2                       # 偽フラッシュの容量 [MB]
MAC = "02:00:00:00:00:01"
DAY = date(2026, 9, 30)
FLASH_ID_OUTPUT = f"""Chip type:          ESP32-D0WDQ6-V3 (revision v3.1)
MAC:                {MAC}
Detected flash size: {FLASH_MB}MB
"""


class FakeEsptool:
    """esptool の代わり。偽フラッシュの中身を返し、指定区間で決まった回数だけ失敗する."""

    def __init__(self, fail_times: dict[int, int] | None = None, corrupt_verify: bool = False
                 ) -> None:
        """偽フラッシュを用意する.

        Args:
            fail_times: 区間の先頭アドレス [bytes] → 失敗させる回数。
            corrupt_verify: True なら照合を常に不一致にする。
        """
        self.flash = bytes(i % 251 for i in range(FLASH_MB * bf.MB))
        self.fail_times = dict(fail_times or {})
        self.corrupt_verify = corrupt_verify
        self.reads: list[int] = []

    def __call__(self, cmd: list[str], timeout_sec: int) -> subprocess.CompletedProcess[str]:
        """コマンドを真似る."""
        if "flash-id" in cmd:
            return subprocess.CompletedProcess(cmd, 0, FLASH_ID_OUTPUT)
        if "read-flash" in cmd:
            i = cmd.index("read-flash")
            offset, size, path = int(cmd[i + 1], 16), int(cmd[i + 2], 16), Path(cmd[i + 3])
            self.reads.append(offset)
            if self.fail_times.get(offset, 0) > 0:
                self.fail_times[offset] -= 1
                path.write_bytes(self.flash[offset:offset + size // 2])  # 途中で欠ける
                return subprocess.CompletedProcess(cmd, 2, "Corrupt data")
            path.write_bytes(self.flash[offset:offset + size])
            return subprocess.CompletedProcess(cmd, 0, "")
        if "verify-flash" in cmd:
            same = Path(cmd[-1]).read_bytes() == self.flash and not self.corrupt_verify
            out = "Verification successful (digest matched)." if same else "MISMATCH"
            return subprocess.CompletedProcess(cmd, 0 if same else 2, out)
        raise AssertionError(f"想定外のコマンド: {cmd}")


def run(tmp_path: Path, fake: FakeEsptool, *extra: str) -> int:
    """偽 esptool でスクリプトを実行する."""
    code: int = bf.main(["--out-dir", str(tmp_path), "--label", "m5stack-basic", *extra],
                        runner=fake, today=DAY)
    return code


def test_parse_chip_info() -> None:
    """MAC は小文字にそろえ、容量は bytes にする."""
    chip = bf.parse_chip_info(FLASH_ID_OUTPUT)
    assert chip.mac == "02:00:00:00:00:01" and chip.mac_compact == "020000000001"
    assert chip.flash_bytes == FLASH_MB * bf.MB


def test_parse_chip_info_rejects_missing_fields() -> None:
    """接続失敗などで MAC が出ていなければ例外."""
    with pytest.raises(ValueError):
        bf.parse_chip_info("A fatal error occurred: Failed to connect")


def test_chunk_offsets_and_invalid_sizes() -> None:
    """16MB は 1MB ずつ 16 区間。割り切れない容量は拒む."""
    assert bf.chunk_offsets(16 * bf.MB) == [i * bf.MB for i in range(16)]
    for bad in (0, bf.MB + 1):
        with pytest.raises(ValueError):
            bf.chunk_offsets(bad)


def test_backup_name_rejects_unsafe_label() -> None:
    """ラベルにパス区切りなどは使わせない."""
    chip = bf.parse_chip_info(FLASH_ID_OUTPUT)
    assert bf.backup_name("m5stack-basic", chip, DAY) == "m5stack-basic_020000000001_20260930.bin"
    with pytest.raises(ValueError):
        bf.backup_name("../x", chip, DAY)


def test_retries_failed_chunk_and_saves_verified_image(tmp_path: Path) -> None:
    """欠けた区間だけ読み直し、照合一致なら .bin と .sha256 を残して作業用の区間を消す."""
    fake = FakeEsptool(fail_times={bf.MB: 1})
    assert run(tmp_path, fake) == 0
    assert fake.reads == [0, bf.MB, bf.MB]                   # 2 区間目だけ 2 回
    out = tmp_path / "m5stack-basic_020000000001_20260930.bin"
    assert out.read_bytes() == fake.flash
    digest = hashlib.sha256(fake.flash).hexdigest()
    assert Path(f"{out}.sha256").read_text(encoding="utf-8") == f"{digest}  {out.name}\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == [out.name, f"{out.name}.sha256"]


def test_resumes_from_saved_chunks(tmp_path: Path) -> None:
    """読み切れずに止まったら、再実行で読み終えた区間は読まない."""
    first = FakeEsptool(fail_times={bf.MB: bf.MAX_TRIES})
    assert run(tmp_path, first) == 1
    second = FakeEsptool()
    assert run(tmp_path, second) == 0
    assert second.reads == [bf.MB]


def test_refuses_to_overwrite(tmp_path: Path) -> None:
    """同じ名前のバックアップがあれば何も読まずに止める."""
    (tmp_path / "m5stack-basic_020000000001_20260930.bin").write_bytes(b"old")
    fake = FakeEsptool()
    assert run(tmp_path, fake) == 1
    assert fake.reads == []


def test_verify_mismatch_leaves_no_image(tmp_path: Path) -> None:
    """照合が合わなければ完成品を残さない."""
    assert run(tmp_path, FakeEsptool(corrupt_verify=True)) == 1
    assert not list(tmp_path.glob("*.bin*"))
