"""記録の CSI の行を送信元ごとにまとめる（ALLCSI のテスト記録用）.

受信機の ALLCSI（firmware/csi-rx）は、送信機以外から来た CSI も出し、1 行の ``<tx>`` に
実際の送信元の MAC を書く。ここでは送信元ごとに、行数・回/秒・方式（11g の LLTF だけか、
11n の HT-LTF か）・RSSI をまとめる。

失敗時の挙動: 記録の長さが正でないときは ValueError。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from csikit.io import RxLine

SIG_MODE_HT = 1  # RxLine.sig_mode: 0 = 11b/g、1 = 11n（HT）
US_PER_SEC = 1_000_000


@dataclass(frozen=True)
class SourceSummary:
    """1 つの送信元のまとめ.

    Attributes:
        tx: 送信元の MAC（コロン区切り。format=1・2 の記録では ``?``）。
        rows: CSI の行数。
        rate_hz: 行数 ÷ 記録の長さ [Hz]。
        n_ht: うち 11n（HT）の行数。
        n_lltf: うち LLTF だけ（11g）の行数。
        rssi_median_dbm: RSSI の中央値 [dBm]。
        rssi_min_dbm: RSSI の最小 [dBm]。
        rssi_max_dbm: RSSI の最大 [dBm]。
        gap_median_ms: 行の間隔の中央値 [ms]（2 行未満なら nan）。
        gap_max_ms: 行の間隔の最大 [ms]（2 行未満なら nan）。
    """

    tx: str
    rows: int
    rate_hz: float
    n_ht: int
    n_lltf: int
    rssi_median_dbm: float
    rssi_min_dbm: int
    rssi_max_dbm: int
    gap_median_ms: float
    gap_max_ms: float


def summarize_sources(lines: list[RxLine], duration_sec: float) -> list[SourceSummary]:
    """送信元ごとにまとめる（行の多い順）.

    Args:
        lines: 記録の CSI の行。
        duration_sec: 記録の長さ [sec]。

    Returns:
        送信元ごとのまとめ。

    Raises:
        ValueError: duration_sec が正でない。
    """
    if duration_sec <= 0:
        raise ValueError(f"記録の長さは正: {duration_sec}")
    groups: dict[str, list[RxLine]] = {}
    for rec in lines:
        groups.setdefault(rec.tx or "?", []).append(rec)
    out = []
    for tx, recs in groups.items():
        rssi = np.array([r.rssi_dbm for r in recs])
        # rx_us は 32 bit で一周するので、差は 2^32 で割った余りを取る
        rx = np.array([r.rx_us for r in recs], dtype=np.int64)
        gaps_ms = (np.diff(rx) % (1 << 32)) / 1000 if len(rx) > 1 else np.array([])
        out.append(SourceSummary(
            tx=tx, rows=len(recs), rate_hz=len(recs) / duration_sec,
            n_ht=sum(r.sig_mode == SIG_MODE_HT for r in recs),
            n_lltf=sum(r.section == "lltf" for r in recs),
            rssi_median_dbm=float(np.median(rssi)), rssi_min_dbm=int(rssi.min()),
            rssi_max_dbm=int(rssi.max()),
            gap_median_ms=float(np.median(gaps_ms)) if len(gaps_ms) else float("nan"),
            gap_max_ms=float(np.max(gaps_ms)) if len(gaps_ms) else float("nan")))
    return sorted(out, key=lambda s: -s.rows)


def mask_mac(mac: str) -> str:
    """MAC の後ろ半分を伏せる（共有するとき用）."""
    parts = mac.split(":")
    if len(parts) != 6:
        return mac
    return ":".join(parts[:3] + ["xx"] * 3)
