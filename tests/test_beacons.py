"""csikit.beacons のテスト（BSCAN の行の読み取りと BSSID ごとのまとめ）."""

from __future__ import annotations

import pytest

from csikit import beacons


def line(ch: int, bssid: str = "aabbccddeeff", ap_ch: int = 6, n: int = 10, csi: int = 0,
         sig_mode: int = 0, rate: int = 0, rssi: int = -50, ssid_hex: str = "686f6d65") -> str:
    """BSCAN の 1 行を作る（既定は ch6 の 11b のビーコン・SSID は home）."""
    return (f"# beacon ch={ch} bssid={bssid} ap_ch={ap_ch} n={n} csi={csi} sig_mode={sig_mode} "
            f"rate={rate} ht_cap=1 interval_tu=100 rssi_mean_dbm={rssi} ssid_hex={ssid_hex}")


def test_parse_beacon_line() -> None:
    """項目を読み、SSID を 16 進から戻す。ビーコンの行でなければ None."""
    o = beacons.parse_beacon_line(line(5, n=7, csi=2, rssi=-63))
    assert o is not None
    assert (o.tuned_ch, o.ap_ch, o.n, o.n_csi, o.rssi_mean_dbm) == (5, 6, 7, 2, -63)
    assert o.ssid == b"home" and o.ht_cap
    assert beacons.parse_beacon_line("# bscan ch=5 dwell_ms=1000 sources=1 overflow=0") is None
    hidden = beacons.parse_beacon_line(line(6, ssid_hex=""))
    assert hidden is not None and hidden.ssid == b""


@pytest.mark.parametrize("bad", [
    "# beacon ch=1 bssid=aabbccddeeff",                 # 項目が足りない
    line(1, bssid="aabbcc"),                            # BSSID の形が違う
    line(1).replace("n=10", "n=x"),                     # 数値でない
    line(1, ssid_hex="6"),                              # 16 進が奇数桁
])
def test_parse_beacon_line_rejects_bad(bad: str) -> None:
    """形の合わないビーコンの行は ValueError."""
    with pytest.raises(ValueError):
        beacons.parse_beacon_line(bad)


@pytest.mark.parametrize("sig_mode, rate, expected", [
    (0, 0, "11b"), (0, 7, "11b"), (0, 11, "11g"), (0, 8, "11g"), (1, 0, "11n"), (0, 4, "不明"),
    (2, 0, "不明"),
])
def test_modulation(sig_mode: int, rate: int, expected: str) -> None:
    """11b は符号 0〜7（4 は無い）、11g は 8〜15、sig_mode=1 は 11n."""
    assert beacons.modulation(sig_mode, rate) == expected


def test_summarize_picks_declared_channel_row() -> None:
    """隣のチャンネルで聞こえた行ではなく、ビーコンに書かれたチャンネルの行を代表にする."""
    obs = [beacons.parse_beacon_line(s) for s in (
        line(5, n=4, rssi=-70), line(6, n=10, csi=5, rssi=-60, rate=11), line(7, n=12, rssi=-75),
        line(11, bssid="112233445566", ap_ch=11, n=9, rssi=-40, ssid_hex=""),
    )]
    rows = beacons.summarize([o for o in obs if o is not None], dwell_ms=1000)
    assert [r.bssid for r in rows] == ["112233445566", "aabbccddeeff"]  # RSSI の強い順
    home = rows[1]
    assert home.ap_ch == 6 and home.heard_on == (5, 6, 7)
    assert home.beacons_per_sec == pytest.approx(10.0)
    assert home.csi_per_sec == pytest.approx(5.0)
    assert home.modulation == "11g" and home.rate_mbps == 6.0 and home.beacon_csi
    assert not rows[0].beacon_csi and beacons.ssid_text(rows[0].ssid) == "（隠す）"


def test_summarize_without_declared_channel_and_bad_dwell() -> None:
    """チャンネルが書かれていなければ、最も多く聞こえたチャンネル。dwell が 0 なら ValueError."""
    obs = [beacons.parse_beacon_line(line(c, ap_ch=0, n=n)) for c, n in ((3, 2), (4, 8))]
    rows = beacons.summarize([o for o in obs if o is not None], dwell_ms=2000)
    assert rows[0].ap_ch == 4 and rows[0].beacons_per_sec == pytest.approx(4.0)
    with pytest.raises(ValueError):
        beacons.summarize([], dwell_ms=0)


def test_mask_and_ssid_text() -> None:
    """BSSID は前半だけ残す。SSID は UTF-8 で読み、壊れた分は置き換える."""
    assert beacons.mask_bssid("aabbccddeeff") == "aabbccxxxxxx"
    assert beacons.ssid_text("家".encode()) == "家"
    assert beacons.ssid_text(b"\x00\x00") == "（隠す）"
    assert beacons.ssid_text(b"\xff") == "�"
