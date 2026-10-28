"""ESP32 のフラッシュを丸ごと読み出して保存する（借用機材を返却時に元へ戻すため）.

    uv run python scripts/backup_flash.py                        # /dev/ttyACM0 の機器を保存
    uv run python scripts/backup_flash.py --port /dev/ttyACM1 --label m5stack-basic

保存先は ``data/firmware-backup/<label>_<MAC>_<日付>.bin``（git 管理外・暗号化バックアップの対象）。
隣に ``.sha256`` を書く。MAC とフラッシュ容量はチップから読み取る。

Crostini の USB 経由では 16MB を一度に読むと途中でデータが欠けて失敗することがある
（921600bps・460800bps とも実際に失敗した）。そこで 1MB ずつ読み、失敗した区間だけ読み直す。
読み終えた区間は ``.parts-<MAC>/`` に残るので、中断しても同じコマンドで続きから再開できる。
最後に ``verify-flash`` でチップ側と照合し、一致したときだけ完成とする。

元に戻すとき（返却前）:

    uvx esptool --port /dev/ttyACM0 -b 460800 write-flash 0 data/firmware-backup/<ファイル>.bin

esptool はプロジェクトの依存に入れず ``uvx esptool`` で呼ぶ（``--esptool`` で変更可）。

終了コード: 0 = 保存・照合とも成功 / 1 = 失敗（既存ファイルがある・読み出し失敗・照合不一致など）
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shlex
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_DIR = ROOT / "data" / "firmware-backup"
DEFAULT_PORT = "/dev/ttyACM0"
DEFAULT_BAUD = 460800            # [bps] 921600 は Crostini で取りこぼしが多かった
DEFAULT_LABEL = "esp32"
DEFAULT_ESPTOOL = "uvx esptool"
CHUNK_BYTES = 0x100000           # 1 区間 = 1MB [bytes]
MAX_TRIES = 4                    # 1 区間あたりの試行回数
MB = 1024 * 1024                 # [bytes]
TIMEOUT_INFO_SEC = 60            # チップ情報の読み取り [sec]
TIMEOUT_CHUNK_SEC = 180          # 1 区間の読み出し [sec]
TIMEOUT_VERIFY_SEC = 300         # 全体の照合 [sec]

MAC_PATTERN = re.compile(r"^MAC:\s*([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})", re.MULTILINE)
FLASH_PATTERN = re.compile(r"Detected flash size:\s*(\d+)MB")
VERIFY_OK = "Verification successful"

Runner = Callable[[list[str], int], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class ChipInfo:
    """チップから読み取った情報.

    Attributes:
        mac: MAC アドレス（小文字・コロン区切り）。
        flash_bytes: フラッシュ容量 [bytes]。
    """

    mac: str
    flash_bytes: int

    @property
    def mac_compact(self) -> str:
        """ファイル名用に区切りを除いた MAC（例: ``020000000001``）."""
        return self.mac.replace(":", "")


def run_command(cmd: list[str], timeout_sec: int) -> subprocess.CompletedProcess[str]:
    """コマンドを実行し、標準出力と標準エラーをまとめて返す.

    Args:
        cmd: 実行するコマンド。
        timeout_sec: 打ち切るまでの時間 [sec]。

    Returns:
        実行結果（``stdout`` に標準エラーも含む）。
    """
    return subprocess.run(cmd, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, timeout=timeout_sec, check=False)


def parse_chip_info(output: str) -> ChipInfo:
    """``esptool flash-id`` の出力から MAC とフラッシュ容量を読む.

    Args:
        output: esptool の出力。

    Returns:
        チップ情報。

    Raises:
        ValueError: MAC またはフラッシュ容量が出力に見つからない。
    """
    mac = MAC_PATTERN.search(output)
    flash = FLASH_PATTERN.search(output)
    if mac is None or flash is None:
        raise ValueError("esptool の出力から MAC・フラッシュ容量を読み取れません")
    return ChipInfo(mac=mac.group(1).lower(), flash_bytes=int(flash.group(1)) * MB)


def chunk_offsets(total_bytes: int, chunk_bytes: int = CHUNK_BYTES) -> list[int]:
    """読み出す区間の先頭アドレスを並べる.

    Args:
        total_bytes: フラッシュ容量 [bytes]。
        chunk_bytes: 1 区間の大きさ [bytes]。

    Returns:
        各区間の先頭アドレス [bytes]。

    Raises:
        ValueError: 容量が区間の大きさで割り切れない、または正でない。
    """
    if total_bytes <= 0 or chunk_bytes <= 0 or total_bytes % chunk_bytes:
        raise ValueError(f"容量 {total_bytes} を区間 {chunk_bytes} で割り切れません")
    return list(range(0, total_bytes, chunk_bytes))


def backup_name(label: str, chip: ChipInfo, day: date) -> str:
    """保存ファイル名を作る（例: ``m5stack-basic_020000000001_20260930.bin``）.

    Args:
        label: 機種などの名前。
        chip: チップ情報。
        day: 読み出した日。

    Returns:
        ファイル名。

    Raises:
        ValueError: ラベルにファイル名として使えない文字がある。
    """
    if not re.fullmatch(r"[A-Za-z0-9-]+", label):
        raise ValueError(f"ラベルは英数字とハイフンのみ: {label!r}")
    return f"{label}_{chip.mac_compact}_{day:%Y%m%d}.bin"


def read_chunk(esptool: list[str], port: str, baud: int, offset: int, size: int, part: Path,
               runner: Runner, log: Callable[[str], None]) -> bool:
    """1 区間を読み出す（失敗したら ``MAX_TRIES`` 回まで読み直す）.

    Args:
        esptool: esptool を呼ぶコマンド。
        port: シリアルポート。
        baud: 通信速度 [bps]。
        offset: 区間の先頭アドレス [bytes]。
        size: 区間の大きさ [bytes]。
        part: 保存先。
        runner: コマンドの実行関数。
        log: 進捗の出力先。

    Returns:
        読み出せたら True。
    """
    for attempt in range(1, MAX_TRIES + 1):
        part.unlink(missing_ok=True)
        cmd = [*esptool, "--port", port, "-b", str(baud), "read-flash", hex(offset), hex(size),
               str(part)]
        try:
            result = runner(cmd, TIMEOUT_CHUNK_SEC)
            ok = result.returncode == 0 and part.is_file() and part.stat().st_size == size
        except subprocess.TimeoutExpired:
            ok = False
        log(f"{'OK  ' if ok else 'FAIL'} 0x{offset:08x}（{attempt} 回目）")
        if ok:
            return True
    part.unlink(missing_ok=True)
    return False


def main(argv: list[str] | None = None, runner: Runner = run_command,
         today: date | None = None) -> int:
    """フラッシュを読み出し、照合して保存する.

    Args:
        argv: 引数（省略時は ``sys.argv``）。
        runner: コマンドの実行関数（テストで差し替える）。
        today: ファイル名に使う日付（省略時は今日）。

    Returns:
        終了コード（0: 成功 / 1: 失敗）。
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", default=DEFAULT_PORT)
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD, help="通信速度 [bps]")
    parser.add_argument("--label", default=DEFAULT_LABEL, help="ファイル名の先頭（機種名など）")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--esptool", default=DEFAULT_ESPTOOL, help="esptool を呼ぶコマンド")
    args = parser.parse_args(argv)
    esptool = shlex.split(args.esptool)

    def log(msg: str) -> None:
        print(msg, flush=True)

    try:
        info = runner([*esptool, "--port", args.port, "flash-id"], TIMEOUT_INFO_SEC)
        chip = parse_chip_info(info.stdout)
        name = backup_name(args.label, chip, today or date.today())
        offsets = chunk_offsets(chip.flash_bytes)
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        print(f"チップ情報を読めません（{args.port}）: {e}", file=sys.stderr)
        return 1
    log(f"MAC {chip.mac} / フラッシュ {chip.flash_bytes // MB}MB")

    out = args.out_dir / name
    if out.exists():
        print(f"既にあります（上書きしません）: {out}", file=sys.stderr)
        return 1
    parts_dir = args.out_dir / f".parts-{chip.mac_compact}"
    parts_dir.mkdir(parents=True, exist_ok=True)

    parts: list[Path] = []
    for offset in offsets:
        part = parts_dir / f"{offset:08x}.bin"
        parts.append(part)
        if part.is_file() and part.stat().st_size == CHUNK_BYTES:
            log(f"済   0x{offset:08x}（前回の読み出しを使う）")
            continue
        if not read_chunk(esptool, args.port, args.baud, offset, CHUNK_BYTES, part, runner, log):
            print(f"0x{offset:08x} を {MAX_TRIES} 回とも読めませんでした。"
                  "もう一度実行すると続きから再開します", file=sys.stderr)
            return 1

    tmp = out.with_suffix(".bin.tmp")
    digest = hashlib.sha256()
    with tmp.open("wb") as f:
        for part in parts:
            data = part.read_bytes()
            digest.update(data)
            f.write(data)

    log("チップ側と照合しています…")
    try:
        verify = runner([*esptool, "--port", args.port, "-b", str(args.baud), "verify-flash", "0",
                         str(tmp)], TIMEOUT_VERIFY_SEC)
        verified = verify.returncode == 0 and VERIFY_OK in verify.stdout
    except (OSError, subprocess.TimeoutExpired):
        verified = False
    if not verified:
        tmp.unlink(missing_ok=True)
        print("チップ側と一致しませんでした。区間を読み直すには "
              f"{parts_dir} を消してから再実行してください", file=sys.stderr)
        return 1

    tmp.rename(out)
    Path(f"{out}.sha256").write_text(f"{digest.hexdigest()}  {out.name}\n",
                                         encoding="utf-8")
    for part in parts:
        part.unlink()
    parts_dir.rmdir()
    log(f"保存しました（照合一致）: {out}")
    log(f"sha256 {digest.hexdigest()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
