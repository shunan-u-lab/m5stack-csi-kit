"""csikit.monitor: 受信の窓と集計・受信機の開き方・描画（画面なし）."""

from __future__ import annotations

import queue
import zlib
from pathlib import Path

import matplotlib
import pytest

from csikit import monitor
from tests.fake_boards import FakeDevice, make_opener

matplotlib.use("Agg")  # 画面なしで描く

S_MAC = "020000000001"
R_MAC = "020000000002"
KEY = "00112233445566778899aabbccddeeff"
OTHER = "0200000000ff"
HEX_HTLTF = "".join(f"{(i % 16) + 1:02x}{(i % 7) + 1:02x}" for i in range(64))  # 64 サブキャリア


def csi_line(seq: int, tx: str = S_MAC, rssi: int = -50, rx_us: int | None = None) -> str:
    """CRC つきの CSI 行（format=3・HT-LTF）."""
    t = seq * 10_000 if rx_us is None else rx_us
    body = f"CSI,{t},{tx},{seq},{rssi},-96,11,0,1,0,0,0,0,1,htltf,128,{HEX_HTLTF}"
    return f"{body}*{zlib.crc32(body.encode()):08x}"


def fill(state: monitor.MonitorState, seqs: list[int], start: float = 0.0,
         step: float = 0.01) -> float:
    """連番の行を、連番 × 10 ms の時刻に取り込み（欠けた連番の分は時間が空く）、最後の時刻を返す."""
    now = start
    for seq in seqs:
        now = start + seq * step
        state.feed(csi_line(seq), now)
    return now


def test_rate_loss_rssi_from_the_last_second() -> None:
    """直近 1 秒の受信レートと、連番の欠けを数える."""
    state = monitor.MonitorState(tx_mac="02:00:00:00:00:01")
    seqs = [s for s in range(200) if s % 10 != 5]  # 10 個に 1 個欠ける
    now = fill(state, seqs)
    assert state.rate_hz(now) == pytest.approx(91, abs=2)
    loss = state.loss_pct(now)
    assert loss is not None and loss == pytest.approx(10, abs=1.5)
    assert "RSSI -50 dBm" in state.title(now)


def test_only_the_paired_transmitter_is_used() -> None:
    """組んだ送信機以外の行は数えるだけで、窓に入れない。壊れた行も数えて捨てる."""
    state = monitor.MonitorState(tx_mac="02:00:00:00:00:01")
    state.feed(csi_line(0), 0.0)
    state.feed(csi_line(1, tx=OTHER), 0.01)
    state.feed(csi_line(2)[:-1] + "0", 0.02)  # CRC 不一致
    state.feed("# stats rate_hz=99.8 dropped=3 other=1", 0.03)
    assert len(state.times) == 1 and state.other_tx == 1 and state.bad_crc == 1
    assert state.dropped == 3
    heat = state.heatmap()
    assert heat.shape == (56, 1)  # HT-LTF の使えるサブキャリアは 56 本


def test_silence_is_shown() -> None:
    """受信が止まったら題名に出す."""
    state = monitor.MonitorState(tx_mac="02:00:00:00:00:01")
    assert state.title(0.0).startswith("NO SIGNAL")
    now = fill(state, list(range(10)))
    assert not state.silent(now)
    assert state.title(now + monitor.SILENT_SEC + 1).startswith("NO SIGNAL")


def test_window_is_bounded() -> None:
    """窓は上限を超えて溜めない."""
    state = monitor.MonitorState(tx_mac="02:00:00:00:00:01")
    fill(state, list(range(monitor.MAX_PACKETS + 50)))
    assert len(state.amps) == monitor.MAX_PACKETS


def test_drain_takes_everything_queued() -> None:
    """読み取りのスレッドが積んだ行をすべて取り込む."""
    state = monitor.MonitorState(tx_mac="02:00:00:00:00:01")
    q: queue.Queue[tuple[str, float]] = queue.Queue()
    for seq in range(5):
        q.put((csi_line(seq), seq * 0.01))
    assert monitor.drain(q, state) == 5 and q.empty()


def test_open_receiver_checks_role_and_pairing(monkeypatch: pytest.MonkeyPatch) -> None:
    """受信機であること・組が設定済みであることを確かめる."""
    monkeypatch.setattr(monitor.ports.time, "sleep", lambda sec: None)
    rx = FakeDevice("rx", R_MAC, peer=S_MAC, key=KEY)
    s, board = monitor.open_receiver("p1", make_opener({"p1": rx}))
    assert board.peer_mac == S_MAC
    with pytest.raises(monitor.MonitorError, match="pair-only"):
        monitor.open_receiver("p1", make_opener({"p1": FakeDevice("rx", R_MAC)}))
    with pytest.raises(monitor.MonitorError, match="答えない"):  # 送信機は速さが違い化ける
        monitor.open_receiver("p0", make_opener({"p0": FakeDevice("tx", R_MAC)}))
    with pytest.raises(monitor.MonitorError, match="他のアプリ"):
        monitor.open_receiver("gone", make_opener({}))


def test_view_redraws_without_a_screen(tmp_path: Path) -> None:
    """描き直しが画面なしで動き、図を保存できる（空の窓・同じ値ばかりの窓でも）."""
    state = monitor.MonitorState(tx_mac="02:00:00:00:00:01")
    fig, redraw = monitor.build_view(state)
    redraw(0.0)  # まだ何も無い
    now = fill(state, list(range(300)))
    redraw(now)
    out = tmp_path / "monitor.png"
    fig.savefig(out)
    assert out.stat().st_size > 0
    assert "Hz" in fig.axes[0].get_title()


def test_titles_never_show_mac(tmp_path: Path) -> None:
    """図の題名・窓の題名に MAC を出さない（保存した図から漏れないように）."""
    state = monitor.MonitorState(tx_mac="02:00:00:00:00:01")
    silent = state.title(0.0)
    assert silent.startswith("NO SIGNAL from T1") and "02:00" not in silent
    now = fill(state, list(range(200)))
    assert "02:00" not in state.title(now)
    fig, redraw = monitor.build_view(state, "csikit monitor  R1=p1")
    redraw(now)
    fig.savefig(tmp_path / "m.png")
    assert all("02:00" not in ax.get_title() for ax in fig.axes)


def test_heatmap_and_rssi_share_the_time_axis() -> None:
    """ヒートマップと RSSI の横軸は、どちらも直近 WINDOW_SEC 秒（窓より長く持っていても揃える）."""
    state = monitor.MonitorState(tx_mac="02:00:00:00:00:01")
    now = fill(state, list(range(1400)))  # 14 秒分（窓 10 秒より長い）
    fig, redraw = monitor.build_view(state)
    redraw(now)
    ax_map, ax_rssi = fig.axes[0], fig.axes[1]  # 3 つ目は色の目盛り
    assert ax_map.get_xlim() == ax_rssi.get_xlim() == (-monitor.WINDOW_SEC, 0)
