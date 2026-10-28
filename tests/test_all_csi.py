"""ALLCSI のテスト: csikit.rxport・csikit.sources と scripts/all_csi.py（偽物のポート）."""

from __future__ import annotations

import importlib.util
import sys
import zlib
from pathlib import Path
from types import ModuleType

import numpy as np
import pytest

from csikit import io, rxport, sources

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "all_csi.py"
T1 = "020000000001"
ROUTER = "aabbcc000001"
LTF_BYTES = 128  # LLTF か HT-LTF の 1 区間 [byte]


def load_script() -> ModuleType:
    """scripts/ はパッケージではないので、ファイルから読み込む."""
    spec = importlib.util.spec_from_file_location("all_csi", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["all_csi"] = module
    spec.loader.exec_module(module)
    return module


ex = load_script()


def csi_line(rx_us: int, tx: str, sig_mode: int = 1, rssi: int = -50) -> str:
    """CRC 付きの format=3 の 1 行（11n なら htltf、11g なら lltf）."""
    section = "htltf" if sig_mode == 1 else "lltf"
    raw = np.ones(LTF_BYTES, dtype=np.int8)
    body = (f"CSI,{rx_us},{tx},-1,{rssi},-95,2,0,{sig_mode},0,0,0,0,0,{section},{raw.size},"
            f"{raw.tobytes().hex()}")
    return f"{body}*{zlib.crc32(body.encode()):08x}"


class FakePort:
    """書いた命令を控え、決めた行を順に返すポート."""

    def __init__(self, lines: list[str]) -> None:
        """返す行を受け取る."""
        self.lines = [(s + "\n").encode() for s in lines]
        self.written: list[str] = []

    def write(self, data: bytes) -> int:
        """命令を控える."""
        self.written.append(data.decode().strip())
        return len(data)

    def readline(self) -> bytes:
        """次の行を返す（無くなったら空）."""
        return self.lines.pop(0) if self.lines else b""


# ---- rxport ----

def test_command_returns_ack_and_passes_other_lines() -> None:
    """応答（ack）までの行は on_line に渡す."""
    seen: list[str] = []
    port = FakePort([csi_line(1, T1), "# ack cmd=ALLCSI on=1 rows=0 channel=11"])
    line = rxport.command(port, "ALLCSI ON", on_line=seen.append)
    assert line.startswith("# ack cmd=ALLCSI on=1")
    assert port.written == ["ALLCSI ON"] and seen[0].startswith("CSI,")


def test_command_raises_on_err_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """# err なら CommandError、応答が無ければ TimeoutError."""
    with pytest.raises(rxport.CommandError, match="recording"):
        rxport.command(FakePort(["# err cmd=ALLCSI reason=recording"]), "ALLCSI ON")
    now = iter(range(0, 1000, 10))
    monkeypatch.setattr(rxport.time, "monotonic", lambda: float(next(now)))
    with pytest.raises(TimeoutError):
        rxport.command(FakePort([]), "WHO")


def test_who_reads_channel_and_tx() -> None:
    """WHO の応答から今のチャンネルと主の送信機を読む."""
    port = FakePort([f"# who role=rx mac=020000000002 channel=11 format=3 tx_mac={T1} relay=1"])
    assert rxport.who(port) == (11, T1)
    with pytest.raises(ValueError):
        rxport.who(FakePort(["# who role=rx"]))


# ---- sources ----

def test_summarize_sources_groups_by_sender() -> None:
    """送信元ごとに行数・回/秒・方式・間隔をまとめ、多い順に並べる."""
    lines = [io.parse_rx_line(csi_line(i * 10_000, T1)) for i in range(5)]
    lines += [io.parse_rx_line(csi_line(i * 300_000, ROUTER, sig_mode=0, rssi=-60 - i))
              for i in range(2)]
    rows = sources.summarize_sources(lines, duration_sec=1.0)
    assert [r.rows for r in rows] == [5, 2]
    s2, router = rows
    assert s2.rate_hz == pytest.approx(5.0) and s2.n_ht == 5 and s2.n_lltf == 0
    assert s2.gap_median_ms == pytest.approx(10.0)
    assert router.n_lltf == 2 and router.rssi_min_dbm == -61
    assert router.gap_max_ms == pytest.approx(300.0)
    with pytest.raises(ValueError):
        sources.summarize_sources(lines, 0.0)


def test_summarize_sources_wraps_rx_us_and_single_row() -> None:
    """rx_us が 32 bit で一周しても間隔は正。1 行だけなら間隔は nan."""
    lines = [io.parse_rx_line(csi_line(2**32 - 5_000, T1)), io.parse_rx_line(csi_line(5_000, T1)),
             io.parse_rx_line(csi_line(0, ROUTER))]
    rows = {r.tx.replace(":", ""): r for r in sources.summarize_sources(lines, 1.0)}
    assert rows[T1].gap_max_ms == pytest.approx(10.0)
    assert np.isnan(rows[ROUTER].gap_median_ms)
    assert sources.mask_mac("aa:bb:cc:00:00:01") == "aa:bb:cc:xx:xx:xx"


# ---- scripts/all_csi.py ----

def test_record_writes_lines_and_counts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CSI と # の行を書き、壊れた行は数えて捨て、dropped の増えた分を返す."""
    port = FakePort(["# stats rate_hz=99 dropped=3", csi_line(0, T1), csi_line(1, ROUTER),
                     "CSI,broken*00000000", "# stats rate_hz=99 dropped=10"])
    ticks = iter([0.0] + [0.1 * i for i in range(1, 50)])
    monkeypatch.setattr(ex.time, "monotonic", lambda: next(ticks))
    out = tmp_path / "a.csv"
    lines, bad, dropped = ex.record(port, 2.0, out)
    assert len(lines) == 2 and bad == 1 and dropped == 7
    assert io.transmitters_in(out) == {"02:00:00:00:00:01": 1, "aa:bb:cc:00:00:01": 1}


def test_restore_turns_off_and_moves_back() -> None:
    """ALLCSI をオフにし、移したチャンネルを戻す（応答が無くても止まらない）."""
    port = FakePort(["# ack cmd=ALLCSI on=0 rows=4 channel=2", "# ack cmd=CHLOCAL channel=11"])
    ex.restore(port, moved_from=11)
    assert port.written == ["ALLCSI OFF", "CHLOCAL 11"]


def test_format_table_marks_t1_and_masks() -> None:
    """T1 の行に印を付け、--mask では MAC の後ろ半分を伏せる."""
    rows = sources.summarize_sources([io.parse_rx_line(csi_line(0, T1)),
                                      io.parse_rx_line(csi_line(0, ROUTER))], 1.0)
    shown = ex.format_table(rows, T1, mask=False)
    assert "T1（送信機）" in shown and "aa:bb:cc:00:00:01" in shown
    assert "aa:bb:cc:xx:xx:xx" in ex.format_table(rows, T1, mask=True)


def test_main_rejects_bad_args(tmp_path: Path) -> None:
    """範囲外の長さ・チャンネル、無いポートは終了コード 2."""
    assert ex.main(["--duration", "0"]) == 2
    assert ex.main(["--duration", "10", "--channel", "14"]) == 2
    assert ex.main(["--duration", "10", "--port", str(tmp_path / "nope")]) == 2
