"""ビルド済みのファーム（1 つにまとめた .bin）を esptool で M5Stack Basic に書き込む.

.bin はブートローダ・パーティション表・boot_app0・本体を 1 つにまとめたもので、先頭（0x0）に書く。
作り方は ``scripts/make_dist.py``（リリースでは CI が作る）。置き場は ``out/firmware/``。

- 書き込みは元の中身（買ったときの M5Stack のデモなど）を消す。残したいときは先に
  ``scripts/backup_flash.py`` で吸い出す
- esptool は ``uv tool install esptool`` で入れたものを使う（docs/decisions/0001）

失敗時の挙動: .bin が無ければ FileNotFoundError（作り方を添える）、esptool が無ければ
FileNotFoundError（入れ方を添える）、書き込みに失敗すれば FlashError（esptool の終了コードつき）。
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DIST_DIR = REPO_ROOT / "out" / "firmware"  # まとめた .bin の置き場（git 管理外）
BOARD = "m5stack-basic"  # platformio.ini の env 名
IMAGE_NAMES = {"tx": f"csi-tx-{BOARD}.bin", "rx": f"csi-rx-{BOARD}.bin"}
FLASH_BAUD = 460800  # 書き込みの速さ [bit/s]（platformio.ini の upload_speed と同じ）
CHIP = "esp32"
IMAGE_OFFSET = "0x0"  # まとめた .bin を書く位置
ESPTOOL = "esptool"


class FlashError(RuntimeError):
    """書き込みに失敗した."""


def default_image(role: str, dist_dir: Path = DIST_DIR) -> Path:
    """役割（tx/rx）の .bin の既定の置き場所."""
    return dist_dir / IMAGE_NAMES[role]


def check_image(path: Path) -> Path:
    """.bin があるかを確かめる.

    Raises:
        FileNotFoundError: 無い（作り方を添える）。
    """
    if not path.is_file():
        raise FileNotFoundError(
            f"ファームの .bin がありません: {path}\n"
            "  リリースの .bin を out/firmware/ に置くか、自分でビルドして作る:\n"
            "  uv run python scripts/make_dist.py --build")
    return path


def find_esptool(which: Callable[[str], str | None] = shutil.which) -> str:
    """Esptool の実行ファイルを探す.

    Raises:
        FileNotFoundError: 見つからない（入れ方を添える）。
    """
    found = which(ESPTOOL)
    if found is None:
        raise FileNotFoundError("esptool が見つかりません。入れ方: uv tool install esptool"
                                "（入れた後、新しい端末を開く）")
    return found


def write_command(esptool: str, port: str, image: Path) -> list[str]:
    """書き込みの esptool のコマンドを作る."""
    return [esptool, "--chip", CHIP, "--port", port, "--baud", str(FLASH_BAUD),
            "write-flash", IMAGE_OFFSET, str(image)]


def write_image(port: str, image: Path, esptool: str,
                run: Callable[[Sequence[str]], int] | None = None) -> None:
    """1 台に書き込む（esptool の出力はそのまま端末に出す）.

    Args:
        port: ポート名。
        image: まとめた .bin。
        esptool: esptool の実行ファイル。
        run: コマンドを実行して終了コードを返す関数（テスト用）。

    Raises:
        FlashError: esptool が失敗した。
    """
    cmd = write_command(esptool, port, check_image(image))
    if run is None:
        def run(c: Sequence[str]) -> int:
            return subprocess.run(list(c), check=False).returncode
    code = run(cmd)
    if code != 0:
        raise FlashError(f"{port} への書き込みに失敗しました（esptool の終了コード {code}）。"
                         "ケーブル・ポート名・他のアプリがポートを使っていないかを確認")
