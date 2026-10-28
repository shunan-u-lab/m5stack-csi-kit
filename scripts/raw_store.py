r"""記録（data/raw）を zstd で圧縮する・解析用の Parquet の写しを作る（ADR 0004）.

    uv run python scripts/raw_store.py compress <記録.csv...>
    uv run python scripts/raw_store.py compress <記録.csv...> --delete-original
    uv run python scripts/raw_store.py parquet <記録.csv.zst...>

compress（.csv.zst を作る。元の CSV は既定で残す）:
- ``<記録名>.csv.zst.tmp`` に書き、展開し直して元の CSV と SHA-256 が一致することを
  確かめてから ``<記録名>.csv.zst`` に改名する。一致しなければ消して失敗にする
- 元の CSV は ``--delete-original`` を付けたときだけ消す（一致を確かめた後）
- 直近 ``RECENT_SEC`` 秒以内に書き換えられた CSV は、記録中かもしれないので飛ばす
- ``.csv.zst`` が既にあれば、中身が元と一致するかだけ確かめる（上書きしない）

parquet（解析用の写しを作る）:
- ``io.load_rx`` で読んだ結果を ``<out-dir>/<記録名>.<ltf>.parquet`` に書き、
  読み戻して同じ値か確かめる。既定の置き場は data/processed/parquet
- 写しが元より新しければ飛ばす（``--force`` で作り直す）

失敗時の挙動: 失敗したファイルがあれば最後に一覧を出し、終了コード 1。
ほかのファイルは続けて処理する。
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np

from csikit import io
from csikit.rawzst import (
    DEFAULT_LEVEL,
    MAX_LEVEL,
    compress_verified,
    zst_matches,
    zst_path_for,
)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PARQUET_DIR = ROOT / "data" / "processed" / "parquet"
RECENT_SEC = 120.0  # これより最近書き換えられた CSV は記録中とみなして飛ばす [sec]
BYTES_PER_MB = 1e6


def compress_one(csv_path: Path, level: int, delete_original: bool,
                 now: Callable[[], float] = time.time) -> str:
    """1 本を圧縮し、一致を確かめる（任意で元を消す）.

    Args:
        csv_path: 元の CSV。
        level: zstd の圧縮の強さ。
        delete_original: 一致を確かめた後に元を消すか。
        now: 今の時刻 [sec]（テストで差し替える）。

    Returns:
        結果の説明（1 行）。

    Raises:
        ValueError: 記録中とみなした・圧縮した中身が一致しない場合。
        OSError: 読み書きできない場合。
    """
    zst = zst_path_for(csv_path)
    age_sec = now() - csv_path.stat().st_mtime
    if age_sec < RECENT_SEC:
        raise ValueError(f"{age_sec:.0f} 秒前に書き換えられています（記録中？）。飛ばします")
    size = csv_path.stat().st_size
    if zst.exists():
        if not zst_matches(csv_path, zst):
            raise ValueError(f"既にある {zst.name} の中身が元と一致しません（上書きしません）")
        note = "既にあり一致"
    else:
        compress_verified(csv_path, level)
        note = "圧縮・一致"
    out = (f"{csv_path.name}: {note}・{size / BYTES_PER_MB:.1f} → "
           f"{zst.stat().st_size / BYTES_PER_MB:.1f} MB")
    if delete_original:
        csv_path.unlink()
        out += "・元を消した"
    return out


def parquet_one(path: Path, out_dir: Path, ltf: io.Ltf, tx: str | None, force: bool) -> str:
    """1 本の Parquet の写しを作り、読み戻して同じ値か確かめる.

    Args:
        path: 記録（``.csv`` か ``.csv.zst``）。
        out_dir: 写しの置き場。
        ltf: 区間。
        tx: 送信機の MAC（None なら ``io.load_rx_csv`` の規則で決める）。
        force: 写しが新しくても作り直すか。

    Returns:
        結果の説明（1 行）。

    Raises:
        ValueError: 読めない・読み戻した値が違う場合。
        OSError: 読み書きできない場合。
    """
    out = io.parquet_path(out_dir, io.recording_name(path), ltf)
    if out.exists() and not force and out.stat().st_mtime >= path.stat().st_mtime:
        return f"{path.name}: 写しが新しいので飛ばす（{out.name}）"
    cap = io.load_rx(path, ltf=ltf, tx=tx)
    io.save_rx_parquet(cap, out, source=path.name)
    back = io.load_rx_parquet(out)
    same = (np.array_equal(cap.csi, back.csi, equal_nan=True)
            and np.array_equal(cap.seq, back.seq)
            and np.array_equal(cap.time_sec, back.time_sec)
            and np.array_equal(cap.rssi_dbm, back.rssi_dbm)
            and np.array_equal(cap.noise_floor_dbm, back.noise_floor_dbm)
            and np.array_equal(cap.subcarrier_index, back.subcarrier_index)
            and cap.tx == back.tx)
    if not same:
        out.unlink(missing_ok=True)
        raise ValueError("Parquet から読み戻した値が元と違います（消しました）")
    return f"{path.name}: {out.name}・{out.stat().st_size / BYTES_PER_MB:.1f} MB・{len(cap.seq)} 行"


def main(argv: list[str] | None = None) -> int:
    """コマンドラインから圧縮・写しの作成をする.

    Args:
        argv: 引数（省略時は ``sys.argv``）。

    Returns:
        終了コード（0: すべて成功 / 1: 失敗したファイルがある・引数の誤り）。
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    pc = sub.add_parser("compress", help="CSV を .csv.zst に圧縮する")
    pc.add_argument("files", type=Path, nargs="+", help="記録（.csv）")
    pc.add_argument("--level", type=int, default=DEFAULT_LEVEL, help="zstd の圧縮の強さ（1〜22）")
    pc.add_argument("--delete-original", action="store_true",
                    help="一致を確かめた後に元の CSV を消す")
    pp = sub.add_parser("parquet", help="解析用の Parquet の写しを作る")
    pp.add_argument("files", type=Path, nargs="+", help="記録（.csv・.csv.zst）")
    pp.add_argument("--out-dir", type=Path, default=DEFAULT_PARQUET_DIR)
    pp.add_argument("--ltf", choices=["htltf", "lltf"], default="htltf")
    pp.add_argument("--tx", help="送信機の MAC（送信機が 2 台の記録で選ぶ）")
    pp.add_argument("--force", action="store_true", help="写しが新しくても作り直す")
    args = parser.parse_args(argv)

    if args.command == "compress" and not 1 <= args.level <= MAX_LEVEL:
        print(f"--level は 1〜{MAX_LEVEL}: {args.level}", file=sys.stderr)
        return 1
    failed: list[str] = []
    for path in args.files:
        try:
            if not path.is_file():
                raise FileNotFoundError(f"ファイルがありません: {path}")
            if args.command == "compress":
                print(compress_one(path, args.level, args.delete_original), flush=True)
            else:
                print(parquet_one(path, args.out_dir, args.ltf, args.tx, args.force), flush=True)
        except (OSError, ValueError) as e:
            print(f"失敗: {path.name}: {e}", file=sys.stderr)
            failed.append(path.name)
    if failed:
        print(f"失敗 {len(failed)} 本: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
