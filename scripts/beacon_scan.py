r"""受信機に ch1〜13 を回らせ、周りのビーコンを BSSID ごとの表にする（BSCAN 命令）.

    uv run python scripts/beacon_scan.py [--port /dev/ttyACM0] [--dwell-ms 1500] [--mask] [--raw]
                                        [--json data/processed/beacon-scan/<名前>.json]

受信機が ch1〜13 を自分で回り（1 チャンネル dwell-ms ずつ）、聞こえたビーコンを数えて
元のチャンネルに戻る（firmware/csi-rx の BSCAN）。この道具は結果を BSSID ごとにまとめ、
ビーコンから CSI が取れる見込み（11b のビーコンには CSI が無い）を付けて表にする。
命令の説明: firmware/csi-rx/README.md の BSCAN

- ``--mask``: BSSID の後ろ半分と SSID を伏せる（画面を共有するとき）
- ``--json``: 結果を保存する（BSSID と SSID を含む。**リポジトリに入れない**。data/ は git の外）

失敗時の挙動: ポートが無い・開けないなら終了コード 2。受信機が断った（SD に記録中・範囲外）・
時間内に終わらない・行が読めないなら終了コード 1。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict
from pathlib import Path

from csikit import beacons, rxport

DEFAULT_DWELL_MS = 1500  # 1 チャンネルにとどまる時間 [ms]（ファームウェアの既定と同じ）
DWELL_MIN_MS = 200  # ファームウェアの BSCAN_DWELL_MIN_MS
DWELL_MAX_MS = 10000  # ファームウェアの BSCAN_DWELL_MAX_MS
N_CHANNELS = 13  # ch1〜13
TIMEOUT_MARGIN_SEC = 10.0  # 走査にかかる時間に足す余裕 [sec]
DONE = re.compile(r"^# bscan done back_to=(\d+) result=(\S+)")
ERR = re.compile(r"^# err cmd=BSCAN (.*)$")


class ScanError(RuntimeError):
    """走査の失敗（受信機が断った・終わらない）."""


def run_scan(port: rxport.Port, dwell_ms: int, raw: bool = False) -> list[beacons.BeaconObs]:
    """BSCAN を送り、終わりの行まで読んでビーコンの行を返す.

    Args:
        port: 開いたポート。
        dwell_ms: 1 チャンネルにとどまる時間 [ms]。
        raw: True なら受信機の ``# bscan``・``# beacon`` の行をそのまま表示する。

    Returns:
        ビーコンの行を読んだもの。

    Raises:
        ScanError: 受信機が断った、時間内に終わらない、元のチャンネルに戻れなかった。
        ValueError: ビーコンの行が読めない。
    """
    port.write(f"BSCAN {dwell_ms}\n".encode("ascii"))
    budget_sec = N_CHANNELS * dwell_ms / 1000 + TIMEOUT_MARGIN_SEC
    deadline = time.monotonic() + budget_sec
    obs: list[beacons.BeaconObs] = []
    while time.monotonic() < deadline:
        line = rxport.read_line(port).strip()
        if not line:
            continue
        if raw and line.startswith(("# bscan", "# beacon")):
            print(line, flush=True)
        if (m := ERR.match(line)) is not None:
            raise ScanError(f"受信機が断った: {m.group(1)}")
        if (m := DONE.match(line)) is not None:
            if m.group(2) != "ok":
                raise ScanError(f"元のチャンネルに戻れなかった: {line}")
            return obs
        o = beacons.parse_beacon_line(line)
        if o is not None:
            obs.append(o)
    raise ScanError(f"{budget_sec:.0f} 秒以内に # bscan done が来なかった"
                    "（受信機の版が古い？ WHO の fw= を確認）")


def format_table(rows: list[beacons.BeaconSummary], mask: bool) -> str:
    """まとめを表にする."""
    head = (f"{'BSSID':<13} {'ch':>3} {'聞こえた ch':<14} {'回/秒':>5} {'方式':<4} {'Mbps':>5} "
            f"{'RSSI':>5} {'CSI/秒':>6}  {'ビーコンの CSI':<14} SSID")
    lines = [head]
    for r in rows:
        heard = ",".join(str(c) for c in r.heard_on)
        rate = f"{r.rate_mbps:g}" if r.rate_mbps is not None else "-"
        if r.beacon_csi:
            judge = "取れる見込み"
        else:
            judge = "無い（11b）" if r.modulation == "11b" else "不明"
        bssid = beacons.mask_bssid(r.bssid) if mask else r.bssid
        ssid = "（伏せる）" if mask and r.ssid else beacons.ssid_text(r.ssid)
        lines.append(f"{bssid:<13} {r.ap_ch:>3} {heard:<14} {r.beacons_per_sec:>5.1f} "
                     f"{r.modulation:<4} {rate:>5} {r.rssi_mean_dbm:>5} {r.csi_per_sec:>6.1f}  "
                     f"{judge:<14} {ssid}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """コマンドラインの入口."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", default=rxport.DEFAULT_PORT)
    parser.add_argument("--baud", type=int, default=rxport.DEFAULT_BAUD)
    parser.add_argument("--dwell-ms", type=int, default=DEFAULT_DWELL_MS,
                        help=f"1 チャンネルにとどまる時間 [ms]（{DWELL_MIN_MS}〜{DWELL_MAX_MS}）")
    parser.add_argument("--mask", action="store_true", help="BSSID の後ろ半分と SSID を伏せる")
    parser.add_argument("--raw", action="store_true", help="受信機の行もそのまま出す")
    parser.add_argument("--json", type=Path, help="結果の保存先（リポジトリに入れない）")
    args = parser.parse_args(argv)
    if not DWELL_MIN_MS <= args.dwell_ms <= DWELL_MAX_MS:
        print(f"エラー: --dwell-ms は {DWELL_MIN_MS}〜{DWELL_MAX_MS}", file=sys.stderr)
        return 2
    try:
        port = rxport.open_port(args.port, args.baud)
    except OSError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 2
    scan_sec = N_CHANNELS * args.dwell_ms / 1000
    print(f"走査中（約 {scan_sec:.0f} 秒。その間 CSI は出ない）…", flush=True)
    try:
        obs = run_scan(port, args.dwell_ms, raw=args.raw)
        rows = beacons.summarize(obs, args.dwell_ms)
    except (ScanError, ValueError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    print(format_table(rows, args.mask))
    n_csi = sum(r.beacon_csi for r in rows)
    print(f"\nBSSID {len(rows)} 個。ビーコンから CSI が取れる見込み {n_csi} 個"
          "（11b のビーコンには CSI が無い）")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        data = [{**asdict(r), "ssid": beacons.ssid_text(r.ssid)} for r in rows]
        args.json.write_text(json.dumps({"dwell_ms": args.dwell_ms, "bssids": data},
                                        ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"保存: {args.json}（BSSID・SSID を含む。リポジトリに入れない）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
