"""受信機の BSCAN 命令（ビーコンの走査）の出力を読み、BSSID ごとにまとめる.

受信機（firmware/csi-rx）は ch1〜13 を順に回り、各チャンネルで聞こえたビーコンを 1 行ずつ出す:

    # beacon ch=<今いるチャンネル> bssid=<12 桁> ap_ch=<ビーコンに書かれたチャンネル>
      n=<ビーコンの数> csi=<同じ送信元の CSI の数> sig_mode=<0/1> rate=<速さの符号>
      ht_cap=<0/1> interval_tu=<間隔>
      rssi_mean_dbm=<平均> ssid_hex=<SSID の 16 進（空 = 隠している）>

同じ AP のビーコンは隣のチャンネルにも漏れて聞こえる（2026-10-14: ch2 の AP が ch1〜4・ch6
でも聞こえた）。そこで BSSID ごとに、ビーコンに書かれたチャンネルで受けた行を代表にする。

- ``parse_beacon_line``: 1 行を ``BeaconObs`` にする（ビーコンの行でなければ None）
- ``modulation``: 方式（11b / 11g / 11n）。**11b（DSSS）のフレームには CSI が無い**
- ``summarize``: BSSID ごとに代表の行を選び、回/秒・方式・CSI が取れる見込みをまとめる

失敗時の挙動: 行の形が合わないときは ValueError（ビーコンの行でないときは None）。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

BEACON_PREFIX = "# beacon "
OFDM_RATE_MIN = 8  # rx_ctrl.rate のうち 11g（OFDM）の符号の最小（0〜7 は 11b の DSSS/CCK）
OFDM_RATE_MAX = 15
SIG_MODE_HT = 1  # rx_ctrl.sig_mode: 0 = 11b/g、1 = 11n（HT）
# ESP-IDF の wifi_phy_rate_t（rx_ctrl.rate の符号 → 速さ [Mbps]）。
# 0〜3 = 長いプリアンブル、5〜7 = 短いプリアンブル（11b）、8〜15 = 11g
RATE_MBPS: dict[int, float] = {
    0: 1.0, 1: 2.0, 2: 5.5, 3: 11.0, 5: 2.0, 6: 5.5, 7: 11.0,
    8: 48.0, 9: 24.0, 10: 12.0, 11: 6.0, 12: 54.0, 13: 36.0, 14: 18.0, 15: 9.0,
}
_FIELDS = ("ch", "bssid", "ap_ch", "n", "csi", "sig_mode", "rate", "ht_cap", "interval_tu",
           "rssi_mean_dbm", "ssid_hex")
_KV = re.compile(r"(\w+)=(\S*)")
_BSSID = re.compile(r"[0-9a-f]{12}")


@dataclass(frozen=True)
class BeaconObs:
    """1 つのチャンネルで、1 つの BSSID のビーコンを数えた結果.

    Attributes:
        tuned_ch: 受信機が合わせていたチャンネル。
        bssid: BSSID（12 桁の 16 進・小文字）。
        ap_ch: ビーコンに書かれたチャンネル（0 = 書かれていない）。
        n: ビーコンの数。
        n_csi: 同じ送信元から来た CSI の数（ビーコン以外のフレームも含む）。
        sig_mode: 最後のビーコンの方式（0 = 11b/g、1 = 11n）。
        rate: 最後のビーコンの速さの符号（``RATE_MBPS``）。
        ht_cap: ビーコンに 11n の能力の要素があるか。
        interval_tu: ビーコンの間隔 [TU = 1.024 ms]。
        rssi_mean_dbm: RSSI の平均 [dBm]。
        ssid: SSID（空 = 隠している）。
    """

    tuned_ch: int
    bssid: str
    ap_ch: int
    n: int
    n_csi: int
    sig_mode: int
    rate: int
    ht_cap: bool
    interval_tu: int
    rssi_mean_dbm: int
    ssid: bytes


def parse_beacon_line(line: str) -> BeaconObs | None:
    """``# beacon ...`` の 1 行を読む.

    Args:
        line: 受信機の出力の 1 行。

    Returns:
        読んだ結果。ビーコンの行でなければ None。

    Raises:
        ValueError: ビーコンの行だが、項目が足りない・形が合わない。
    """
    if not line.startswith(BEACON_PREFIX):
        return None
    kv = dict(_KV.findall(line))
    missing = [k for k in _FIELDS if k not in kv]
    if missing:
        raise ValueError(f"ビーコンの行に項目が足りない: {missing}: {line!r}")
    if not _BSSID.fullmatch(kv["bssid"]):
        raise ValueError(f"BSSID の形が違う: {kv['bssid']!r}")
    try:
        ssid = bytes.fromhex(kv["ssid_hex"])
        return BeaconObs(
            tuned_ch=int(kv["ch"]), bssid=kv["bssid"], ap_ch=int(kv["ap_ch"]), n=int(kv["n"]),
            n_csi=int(kv["csi"]), sig_mode=int(kv["sig_mode"]), rate=int(kv["rate"]),
            ht_cap=kv["ht_cap"] == "1", interval_tu=int(kv["interval_tu"]),
            rssi_mean_dbm=int(kv["rssi_mean_dbm"]), ssid=ssid)
    except ValueError as e:
        raise ValueError(f"ビーコンの行の値が読めない: {line!r}") from e


def modulation(sig_mode: int, rate: int) -> str:
    """フレームの方式を返す（``11n`` / ``11g`` / ``11b`` / ``不明``）.

    11b（DSSS・CCK）にはサブキャリアが無いので CSI が無い。11g・11n は OFDM で CSI がある。
    """
    if sig_mode == SIG_MODE_HT:
        return "11n"
    if sig_mode == 0 and OFDM_RATE_MIN <= rate <= OFDM_RATE_MAX:
        return "11g"
    if sig_mode == 0 and rate in RATE_MBPS:
        return "11b"
    return "不明"


@dataclass(frozen=True)
class BeaconSummary:
    """1 つの BSSID のまとめ.

    Attributes:
        bssid: BSSID。
        ssid: SSID（空 = 隠している）。
        ap_ch: ビーコンに書かれたチャンネル（無ければ最も多く聞こえたチャンネル）。
        heard_on: ビーコンが聞こえたチャンネル（昇順）。
        beacons_per_sec: 代表の行のビーコンの回/秒。
        csi_per_sec: 代表の行の、同じ送信元の CSI の回/秒（ビーコン以外のフレームも含む）。
        rssi_mean_dbm: 代表の行の RSSI の平均 [dBm]。
        modulation: ビーコンの方式（``modulation``）。
        rate_mbps: ビーコンの速さ [Mbps]（不明なら None）。
        ht_cap: AP が 11n に対応しているか（ビーコンの要素）。
        beacon_csi: ビーコンから CSI が取れる見込み（11g・11n なら True）。
    """

    bssid: str
    ssid: bytes
    ap_ch: int
    heard_on: tuple[int, ...]
    beacons_per_sec: float
    csi_per_sec: float
    rssi_mean_dbm: int
    modulation: str
    rate_mbps: float | None
    ht_cap: bool
    beacon_csi: bool


def summarize(obs: list[BeaconObs], dwell_ms: int) -> list[BeaconSummary]:
    """BSSID ごとに代表の行を選んでまとめる（RSSI の強い順）.

    代表は、ビーコンに書かれたチャンネルで受けた行。無ければビーコンの数が最も多い行。

    Args:
        obs: ``parse_beacon_line`` の結果。
        dwell_ms: 1 チャンネルにとどまった時間 [ms]。

    Returns:
        BSSID ごとのまとめ。

    Raises:
        ValueError: dwell_ms が正でない。
    """
    if dwell_ms <= 0:
        raise ValueError(f"dwell_ms は正: {dwell_ms}")
    dwell_sec = dwell_ms / 1000
    by_bssid: dict[str, list[BeaconObs]] = {}
    for o in obs:
        by_bssid.setdefault(o.bssid, []).append(o)
    out = []
    for bssid, rows in by_bssid.items():
        declared = Counter(r.ap_ch for r in rows if r.ap_ch > 0)
        ap_ch = declared.most_common(1)[0][0] if declared else max(rows, key=lambda r: r.n).tuned_ch
        on_own = [r for r in rows if r.tuned_ch == ap_ch]
        main = max(on_own or rows, key=lambda r: r.n)
        mod = modulation(main.sig_mode, main.rate)
        ssid = next((r.ssid for r in rows if r.ssid), b"")
        out.append(BeaconSummary(
            bssid=bssid, ssid=ssid, ap_ch=ap_ch, heard_on=tuple(sorted({r.tuned_ch for r in rows})),
            beacons_per_sec=main.n / dwell_sec, csi_per_sec=main.n_csi / dwell_sec,
            rssi_mean_dbm=main.rssi_mean_dbm, modulation=mod,
            rate_mbps=RATE_MBPS.get(main.rate) if main.sig_mode == 0 else None,
            ht_cap=main.ht_cap, beacon_csi=mod in ("11g", "11n")))
    return sorted(out, key=lambda s: -s.rssi_mean_dbm)


def ssid_text(ssid: bytes) -> str:
    """SSID を表示用の文字列にする（隠しているなら ``（隠す）``）."""
    if not ssid or not ssid.strip(b"\x00"):
        return "（隠す）"
    return ssid.decode("utf-8", errors="replace")


def mask_bssid(bssid: str) -> str:
    """BSSID の後ろ半分を伏せる（共有するとき用）。前半はメーカーの番号（OUI）."""
    return bssid[:6] + "xxxxxx"
