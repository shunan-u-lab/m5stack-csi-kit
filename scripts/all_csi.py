r"""受信機（R1）が受けられる CSI をすべて記録し、送信元ごとにまとめる（テスト用・ALLCSI 命令）.

    uv run python scripts/all_csi.py --duration 60 [--channel 2] [--port /dev/ttyACM0] [--mask]
                                     [--out data/processed/all-csi/<名前>.csv]

ふだん受信機は送信機（T1）の CSI だけを出す。この道具は受信機の ALLCSI をオンにし、近くの
ルーター・スマホなど、受信機に届いて CSI が取れたフレームをすべて記録する。1 行の ``<tx>`` が
実際の送信元。終わったら（中断しても）ALLCSI をオフにし、チャンネルを元に戻してから、
送信元ごとの行数・回/秒・方式・RSSI・間隔を表にする。

- ``--channel``: R1 だけをそのチャンネルに移して記録する（CHLOCAL。T1 は動かさない）。
  ALLCSI の間は受信機の番犬が止まるので、T1 が来ないチャンネルにもとどまれる
- 記録は **data/processed/all-csi/**（git の外）に置く。近所の機器の MAC が入るので data/raw・
  リポジトリには入れない。``io.load_rx(path, tx=<MAC>, ltf=...)`` で送信元を選んで読める
- T1 も同時に受けていると、行が増えてシリアルが詰まることがある
  （記録の ``# stats`` の dropped を表示する）

失敗時の挙動: ポートが無い・開けないなら終了コード 2。受信機が断った（SD に記録中・古い版）・
応答が無いなら終了コード 1（ALLCSI はオフに戻す）。
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from csikit import io, rxport, sources

DEFAULT_OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "processed" / "all-csi"
MAX_DURATION_SEC = 600.0  # テスト用なので長くしない [sec]
STATS_DROPPED = re.compile(r"^# stats .*\bdropped=(\d+)")


def record(port: rxport.Port, duration_sec: float,
           out_path: Path) -> tuple[list[io.RxLine], int, int | None]:
    """duration_sec の間、受信機の行をファイルに書き、CSI の行を読んで返す.

    Args:
        port: 開いたポート（ALLCSI をオンにした後）。
        duration_sec: 記録の長さ [sec]。
        out_path: 書き出し先。

    Returns:
        (CSI の行, 読めなかった CSI の行の数,
        記録の間に増えた dropped（# stats が 2 回未満なら None))。
    """
    lines: list[io.RxLine] = []
    bad = 0
    dropped: list[int] = []
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as out:
        out.write(f"# all_csi start={datetime.now().isoformat(timespec='seconds')} "
                  f"duration_sec={duration_sec}\n")
        end = time.monotonic() + duration_sec
        while time.monotonic() < end:
            line = rxport.read_line(port)
            if not line:
                continue
            if line.startswith(io.RX_LINE_PREFIX):
                try:
                    lines.append(io.parse_rx_line(line))
                except ValueError:
                    bad += 1
                    continue
            elif (m := STATS_DROPPED.match(line)) is not None:
                dropped.append(int(m.group(1)))
            out.write(line + "\n")
    return lines, bad, (dropped[-1] - dropped[0]) if len(dropped) >= 2 else None


def format_table(rows: list[sources.SourceSummary], t1_mac: str, mask: bool) -> str:
    """送信元ごとのまとめを表にする."""
    head = (f"{'送信元':<18} {'行':>6} {'回/秒':>6} {'11n':>5} {'11g':>5} {'RSSI 中央':>9} "
            f"{'範囲':>9} {'間隔 中央 ms':>12} {'最大 ms':>8}  備考")
    out = [head]
    for r in rows:
        mac = sources.mask_mac(r.tx) if mask else r.tx
        note = "T1（送信機）" if r.tx.replace(":", "") == t1_mac else ""
        out.append(f"{mac:<18} {r.rows:>6} {r.rate_hz:>6.1f} {r.n_ht:>5} {r.n_lltf:>5} "
                   f"{r.rssi_median_dbm:>9.0f} {r.rssi_min_dbm:>4}〜{r.rssi_max_dbm:<4} "
                   f"{r.gap_median_ms:>12.1f} {r.gap_max_ms:>8.0f}  {note}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    """コマンドラインの入口."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--duration", type=float, required=True, help="記録の長さ [sec]")
    parser.add_argument("--channel", type=int, help="R1 だけをこのチャンネル（1〜13）に移す")
    parser.add_argument("--port", default=rxport.DEFAULT_PORT)
    parser.add_argument("--baud", type=int, default=rxport.DEFAULT_BAUD)
    parser.add_argument("--out", type=Path,
                        help="記録の書き出し先（既定は data/processed/all-csi/）")
    parser.add_argument("--mask", action="store_true", help="表の MAC の後ろ半分を伏せる")
    args = parser.parse_args(argv)
    if not 0 < args.duration <= MAX_DURATION_SEC:
        print(f"エラー: --duration は 0 より大きく {MAX_DURATION_SEC:.0f} 以下", file=sys.stderr)
        return 2
    if args.channel is not None and not 1 <= args.channel <= 13:
        print("エラー: --channel は 1〜13", file=sys.stderr)
        return 2
    out_path = args.out or DEFAULT_OUT_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-all-csi.csv"
    try:
        port = rxport.open_port(args.port, args.baud)
    except OSError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 2

    moved_from: int | None = None
    try:
        back_ch, t1_mac = rxport.who(port)
        rxport.command(port, "ALLCSI ON")
        if args.channel is not None and args.channel != back_ch:
            rxport.command(port, f"CHLOCAL {args.channel}")
            moved_from = back_ch
        where = f"ch{args.channel or back_ch}"
        print(f"記録中（{args.duration:.0f} 秒・{where}）… → {out_path}", flush=True)
        lines, bad, dropped = record(port, args.duration, out_path)
    except (rxport.CommandError, TimeoutError, ValueError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("中断しました（ALLCSI をオフに戻します）", file=sys.stderr)
        return 1
    finally:
        restore(port, moved_from)

    rows = sources.summarize_sources(lines, args.duration)
    print(format_table(rows, t1_mac, args.mask))
    print(f"\n送信元 {len(rows)} 個・CSI の行 {len(lines)}（読めなかった行 {bad}）")
    if dropped:
        print(f"注意: 受信機で捨てた行が {dropped} ありました（シリアルが詰まった）")
    print(f"記録: {out_path}（近所の機器の MAC を含む。リポジトリに入れない）")
    return 0


def restore(port: rxport.Port, moved_from: int | None) -> None:
    """ALLCSI をオフにし、移したチャンネルを戻す（失敗しても続け、警告だけ出す）."""
    steps = ["ALLCSI OFF"] + ([f"CHLOCAL {moved_from}"] if moved_from is not None else [])
    for step in steps:
        try:
            rxport.command(port, step)
        except (rxport.CommandError, TimeoutError) as e:
            print(f"警告: 戻せませんでした（{step}）: {e}。"
                  "R1 を再起動すれば ch11・ALLCSI オフに戻る", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
