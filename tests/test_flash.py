"""csikit.flash と scripts/make_dist.py: 書き込みとまとめた .bin のコマンド."""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

import pytest

from csikit import flash

ROOT = Path(__file__).resolve().parent.parent


def load_make_dist() -> ModuleType:
    """scripts/ はパッケージではないので、ファイルから読み込む."""
    spec = importlib.util.spec_from_file_location("make_dist", ROOT / "scripts" / "make_dist.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["make_dist"] = module
    spec.loader.exec_module(module)
    return module


md = load_make_dist()


def test_write_command_puts_merged_image_at_zero(tmp_path: Path) -> None:
    """まとめた .bin を 0x0 に書く."""
    image = tmp_path / "a.bin"
    assert flash.write_command("esptool", "COM3", image) == [
        "esptool", "--chip", "esp32", "--port", "COM3", "--baud", "460800",
        "write-flash", "0x0", str(image)]


def test_missing_image_and_esptool_say_how_to_fix(tmp_path: Path) -> None:
    """.bin・esptool が無ければ、作り方・入れ方を添える."""
    with pytest.raises(FileNotFoundError, match="make_dist.py --build"):
        flash.check_image(tmp_path / "none.bin")
    with pytest.raises(FileNotFoundError, match="uv tool install esptool"):
        flash.find_esptool(lambda name: None)


def test_write_image_reports_failure(tmp_path: Path) -> None:
    """Esptool が 0 以外で終われば FlashError."""
    image = tmp_path / "a.bin"
    image.write_bytes(b"\xe9")
    ran: list[Sequence[str]] = []
    flash.write_image("p0", image, "esptool", run=lambda c: ran.append(c) or 0)
    assert ran and ran[0][-1] == str(image)
    with pytest.raises(flash.FlashError, match="終了コード 2"):
        flash.write_image("p0", image, "esptool", run=lambda c: 2)


def fake_build(tmp_path: Path) -> tuple[Path, Path]:
    """PlatformIO のビルドの出力と boot_app0 の偽物を作る."""
    core = tmp_path / "pio-core"
    boot = core / md.BOOT_APP0
    boot.parent.mkdir(parents=True)
    boot.write_bytes(b"boot")
    return core, boot


def test_merge_command_lists_parts_in_offset_order(tmp_path: Path) -> None:
    """ブートローダ・パーティション・boot_app0・本体の順に、決まった位置で並べる."""
    items = [("0x1000", tmp_path / "b"), ("0x10000", tmp_path / "f")]
    cmd = md.merge_command("esptool", tmp_path / "o.bin", items)
    assert cmd[:4] == ["esptool", "--chip", "esp32", "merge-bin"]
    assert cmd[-4:] == ["0x1000", str(tmp_path / "b"), "0x10000", str(tmp_path / "f")]
    assert [a for _, a in md.OFFSETS] == ["bootloader.bin", "partitions.bin", "boot_app0.bin",
                                          "firmware.bin"]


def test_make_removes_partial_output_on_failure(tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """2 つ目で失敗したら、1 つ目も消して何も残さない."""
    core, _ = fake_build(tmp_path)
    fw = tmp_path / "fw"
    for role in ("tx", "rx"):
        build = fw / role / ".pio" / "build" / flash.BOARD
        build.mkdir(parents=True)
        for name in ("bootloader.bin", "partitions.bin", "firmware.bin"):
            (build / name).write_bytes(role.encode())
    monkeypatch.setattr(md, "FIRMWARE_DIRS", {"tx": fw / "tx", "rx": fw / "rx"})
    out_dir = tmp_path / "out"

    def run(cmd: Sequence[str], cwd: Path | None) -> int:
        out = Path(cmd[cmd.index("-o") + 1])
        if "csi-rx" in out.name:
            return 1
        out.write_bytes(b"merged")
        return 0

    with pytest.raises(RuntimeError, match="rx"):
        md.make(out_dir, core, "esptool", run)
    assert list(out_dir.iterdir()) == []

    def ok(cmd: Sequence[str], cwd: Path | None) -> int:
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"merged")
        return 0

    made = md.make(out_dir, core, "esptool", ok)
    assert [p.name for p in made] == ["csi-tx-m5stack-basic.bin", "csi-rx-m5stack-basic.bin",
                                      "SHA256SUMS"]
    assert made[2].read_text(encoding="utf-8").count("  csi-") == 2


def test_parts_missing_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ビルドしていなければ、--build を案内する."""
    core, _ = fake_build(tmp_path)
    monkeypatch.setattr(md, "FIRMWARE_DIRS", {"tx": tmp_path / "tx", "rx": tmp_path / "rx"})
    with pytest.raises(FileNotFoundError, match="--build"):
        md.parts("tx", core)
