"""scripts/beacon_scan.py のテスト（偽物のポートで BSCAN のやり取りを確かめる）."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "beacon_scan.py"
BEACON = ("# beacon ch=2 bssid=aabbccddeeff ap_ch=2 n=10 csi=0 sig_mode=0 rate=11 ht_cap=1 "
          "interval_tu=100 rssi_mean_dbm=-50 ssid_hex=686f6d65")


def load_script() -> ModuleType:
    """scripts/ はパッケージではないので、ファイルから読み込む."""
    spec = importlib.util.spec_from_file_location("beacon_scan", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["beacon_scan"] = module
    spec.loader.exec_module(module)
    return module


ex = load_script()


class FakePort:
    """書いた命令を控え、決めた行を順に返すポート."""

    def __init__(self, lines: list[str]) -> None:
        """返す行を受け取る."""
        self.lines = [(s + "\n").encode() for s in lines]
        self.written: list[bytes] = []

    def write(self, data: bytes) -> int:
        """命令を控える."""
        self.written.append(data)
        return len(data)

    def readline(self) -> bytes:
        """次の行を返す（無くなったら空）."""
        return self.lines.pop(0) if self.lines else b""


def test_run_scan_reads_until_done() -> None:
    """BSCAN を送り、CSI の行などは読み飛ばして # bscan done までのビーコンを返す."""
    port = FakePort(["CSI,1,2,3", "# bscan start dwell_ms=500 channels=1-13 back_to=11", BEACON,
                     "# bscan ch=2 dwell_ms=500 sources=1 overflow=0",
                     "# bscan done back_to=11 result=ok", BEACON])
    obs = ex.run_scan(port, 500)
    assert port.written == [b"BSCAN 500\n"]
    assert len(obs) == 1 and obs[0].bssid == "aabbccddeeff"


@pytest.mark.parametrize("lines, match", [
    (["# err cmd=BSCAN reason=recording"], "断った"),
    (["# bscan done back_to=2 result=verify_failed"], "戻れなかった"),
])
def test_run_scan_errors(lines: list[str], match: str) -> None:
    """受信機が断った・元のチャンネルに戻れなかったら ScanError."""
    with pytest.raises(ex.ScanError, match=match):
        ex.run_scan(FakePort(lines), 500)


def test_run_scan_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """終わりの行が来なければ ScanError（時計を進めて待たない）."""
    now = iter(range(0, 1000, 10))
    monkeypatch.setattr(ex.time, "monotonic", lambda: float(next(now)))
    with pytest.raises(ex.ScanError, match="来なかった"):
        ex.run_scan(FakePort([]), 200)


def test_format_table_masks() -> None:
    """--mask では BSSID の後ろ半分と SSID を伏せる."""
    from csikit import beacons

    o = beacons.parse_beacon_line(BEACON)
    assert o is not None
    rows = beacons.summarize([o], 1000)
    shown = ex.format_table(rows, mask=False)
    masked = ex.format_table(rows, mask=True)
    assert "aabbccddeeff" in shown and "home" in shown and "取れる見込み" in shown
    assert "aabbccxxxxxx" in masked and "home" not in masked


def test_main_rejects_bad_dwell_and_missing_port(tmp_path: Path) -> None:
    """範囲外の dwell・無いポートは終了コード 2."""
    assert ex.main(["--dwell-ms", "50"]) == 2
    assert ex.main(["--port", str(tmp_path / "nope")]) == 2
