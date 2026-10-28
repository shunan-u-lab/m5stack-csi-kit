r"""PlatformIO のビルドから、書き込み用の 1 つにまとめた .bin を作る（csikit flash が使う）.

    uv run python scripts/make_dist.py            # ビルド済みのものをまとめる
    uv run python scripts/make_dist.py --build    # 先に pio run でビルドする

作るもの（``out/firmware/``・git 管理外）:

- ``csi-tx-m5stack-basic.bin``・``csi-rx-m5stack-basic.bin``: ブートローダ（0x1000）・
  パーティション表（0x8000）・boot_app0（0xe000）・本体（0x10000）をまとめたもの。先頭（0x0）に書く
- ``SHA256SUMS``: 上の 2 つの sha256（リリースに添える）

失敗時の挙動: ビルドの出力・boot_app0・esptool が見つからない、esptool が失敗したときは、
理由を表示して終了コード 1 で終わる（途中まで作ったファイルは残さない）。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from csikit import flash  # noqa: E402

FIRMWARE_DIRS = {"tx": REPO_ROOT / "firmware" / "csi-tx", "rx": REPO_ROOT / "firmware" / "csi-rx"}
# 書く位置（Arduino-ESP32 2.0.x の既定のパーティション表。PlatformIO の書き込みと同じ）
OFFSETS = (("0x1000", "bootloader.bin"), ("0x8000", "partitions.bin"),
           ("0xe000", "boot_app0.bin"), ("0x10000", "firmware.bin"))
FLASH_MODE = "dio"  # esp32dev の boards/esp32dev.json と同じ
FLASH_FREQ = "40m"
FLASH_SIZE = "4MB"
BOOT_APP0 = (Path("packages") / "framework-arduinoespressif32" / "tools" / "partitions"
             / "boot_app0.bin")
SUMS_NAME = "SHA256SUMS"


def platformio_core_dir() -> Path:
    """PlatformIO の置き場（環境変数 PLATFORMIO_CORE_DIR か ~/.platformio）."""
    env = os.environ.get("PLATFORMIO_CORE_DIR")
    return Path(env) if env else Path.home() / ".platformio"


def parts(role: str, core_dir: Path) -> list[tuple[str, Path]]:
    """まとめる部品（位置, ファイル）を返す.

    Raises:
        FileNotFoundError: 部品が無い（ビルドしていない・PlatformIO の置き場が違う）。
    """
    build = FIRMWARE_DIRS[role] / ".pio" / "build" / flash.BOARD
    found: list[tuple[str, Path]] = []
    for offset, name in OFFSETS:
        path = core_dir / BOOT_APP0 if name == "boot_app0.bin" else build / name
        if not path.is_file():
            raise FileNotFoundError(f"{path} がありません（--build でビルドするか、"
                                    "PLATFORMIO_CORE_DIR を確認）")
        found.append((offset, path))
    return found


def merge_command(esptool: str, out: Path, items: Sequence[tuple[str, Path]]) -> list[str]:
    """Esptool の merge-bin のコマンドを作る."""
    cmd = [esptool, "--chip", flash.CHIP, "merge-bin", "-o", str(out),
           "--flash-mode", FLASH_MODE, "--flash-freq", FLASH_FREQ, "--flash-size", FLASH_SIZE]
    for offset, path in items:
        cmd += [offset, str(path)]
    return cmd


def sha256(path: Path) -> str:
    """ファイルの sha256（16 進）."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make(out_dir: Path, core_dir: Path, esptool: str,
         run: Callable[[Sequence[str], Path | None], int]) -> list[Path]:
    """2 つの .bin と SHA256SUMS を作る.

    Raises:
        FileNotFoundError: 部品が無い。
        RuntimeError: esptool が失敗した。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    made: list[Path] = []
    try:
        for role in ("tx", "rx"):
            out = flash.default_image(role, out_dir)
            code = run(merge_command(esptool, out, parts(role, core_dir)), None)
            if code != 0 or not out.is_file():
                raise RuntimeError(f"esptool merge-bin が失敗しました（{role}・終了コード {code}）")
            made.append(out)
    except (FileNotFoundError, RuntimeError):
        for path in made:
            path.unlink(missing_ok=True)
        raise
    sums = out_dir / SUMS_NAME
    sums.write_text("".join(f"{sha256(p)}  {p.name}\n" for p in made), encoding="utf-8")
    return [*made, sums]


def run_command(cmd: Sequence[str], cwd: Path | None) -> int:
    """コマンドを実行して終了コードを返す（出力はそのまま端末に出す）."""
    return subprocess.run(list(cmd), cwd=cwd, check=False).returncode


def main(argv: list[str] | None = None) -> int:
    """引数を読んで .bin を作る."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--build", action="store_true", help="先に pio run でビルドする")
    parser.add_argument("--out-dir", type=Path, default=flash.DIST_DIR)
    args = parser.parse_args(argv)
    try:
        esptool = flash.find_esptool()
        if args.build:
            pio = shutil.which("pio")
            if pio is None:
                raise FileNotFoundError("pio が見つかりません。入れ方: uv tool install platformio")
            for role, d in FIRMWARE_DIRS.items():
                if run_command([pio, "run"], d) != 0:
                    raise RuntimeError(f"{role} のビルドに失敗しました（{d}）")
        made = make(args.out_dir, platformio_core_dir(), esptool, run_command)
    except (FileNotFoundError, RuntimeError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    for path in made:
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
