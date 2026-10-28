"""scripts/live_csi.py のうち、実機と画面なしで確かめられる部分のテスト."""

from __future__ import annotations

import importlib.util
import io as stdio
import sys
import threading
import time
import zlib
from pathlib import Path
from types import ModuleType

import matplotlib

matplotlib.use("Agg")

import numpy as np  # noqa: E402
import pytest  # noqa: E402

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "live_csi.py"
N_PAIRS = 128  # len=256 → LLTF 64 + HT-LTF 64 サブキャリア
HT_OFFSET = 64  # HT-LTF 区間の先頭（サブキャリア単位）
AMP = 30  # 実部に入れる値（振幅になる）


def load_script() -> ModuleType:
    """scripts/ はパッケージではないので、ファイルから読み込む."""
    spec = importlib.util.spec_from_file_location("live_csi", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["live_csi"] = module
    spec.loader.exec_module(module)
    return module


live = load_script()


def csi_line(rx_us: int, seq: int = 0, amp: int | np.ndarray = AMP, rssi: int = -60) -> bytes:
    """HT-LTF の実部が ``amp``（全サブキャリア共通か 64 要素）の、CRC つき CSI 行（format=2）."""
    raw = np.zeros((N_PAIRS, 2), dtype=np.int8)  # [虚部, 実部]
    raw[HT_OFFSET:, 1] = amp
    body = f"CSI,{rx_us},{seq},{rssi},-96,11,0,1,0,0,0,0,0,256,{raw.tobytes().hex()}"
    return f"{body}*{zlib.crc32(body.encode()):08x}".encode()


def test_buffer_unwraps_time_and_keeps_latest() -> None:
    """32bit の一周をまたいでも時間が進み、最大数を超えたら古いものを捨てる."""
    buf = live.LiveBuffer(max_packets=3)
    recorder = live.Recorder(None)
    start = 2**32 - 10_000
    for i, rx_us in enumerate([start, 0, 10_000, 20_000]):
        buf.append(recorder.handle(csi_line(rx_us % 2**32, seq=i)))
    snap = buf.snapshot()
    np.testing.assert_allclose(snap.time_sec, [0.01, 0.02, 0.03])
    np.testing.assert_array_equal(snap.seq, [1, 2, 3])
    assert snap.amp.shape == (3, 56)
    np.testing.assert_allclose(snap.amp, AMP)


def test_buffer_empty_snapshot_is_none() -> None:
    """何も受け取っていなければ None."""
    assert live.LiveBuffer().snapshot() is None


def test_recorder_writes_valid_lines_and_marks() -> None:
    """正しい行・注記・印だけを書き、壊れた行は数えて捨て、最後に集計を書く."""
    out = stdio.StringIO()
    out.close = lambda: None  # 中身を後で確かめるため閉じない
    recorder = live.Recorder(out)
    good = csi_line(10_000, seq=7)
    assert recorder.handle(b"# CSI receiver format=2") is None
    assert recorder.handle(good[:-1] + b"0") is None  # CRC 不一致
    rec = recorder.handle(good)
    assert rec is not None and rec.seq == 7
    mark = recorder.mark("walk")
    assert mark.startswith("# mark label=walk seq=7 rx_us=10000 wall=")
    recorder.close()
    lines = out.getvalue().splitlines()
    assert lines[:3] == ["# CSI receiver format=2", good.decode(), mark]
    assert lines[3].startswith("# summary ok=1 bad_crc=1")


def test_replay_and_read_loop_fill_buffer(tmp_path: Path) -> None:
    """記録ファイルを待たずに再生すると、全行がバッファに入り、終わりが伝わる."""
    path = tmp_path / "rec.csv"
    lines = [b"# capture start=x"] + [csi_line(i * 10_000, seq=i) for i in range(150)]
    path.write_bytes(b"\n".join(lines) + b"\n")
    buf, recorder, status = live.LiveBuffer(), live.Recorder(None), {}
    chunks = live.replay_chunks(path, threading.Event(), speed=0)
    live.read_loop(chunks, recorder, buf, status, end_message="replay finished")
    assert recorder.stats.ok == 150
    assert status == {"done": "replay finished"}
    assert len(buf.snapshot().time_sec) == 150


def test_view_update_and_marks_without_display() -> None:
    """画面なしでも描画の更新と印が例外なく動き、状態の文に集計が出る."""
    buf, recorder = live.LiveBuffer(), live.Recorder(None)
    rng = np.random.default_rng(0)
    view = live.LiveView(buf, recorder, {}, "test")
    view.update()  # 受信前
    assert "waiting" in view.text.get_text()
    for i in range(250):
        buf.append(recorder.handle(csi_line(i * 10_000, seq=i, amp=int(rng.integers(20, 40)))))
    view.update()
    view.add_mark(1.0, "walk")
    view.update()
    text = view.text.get_text()
    assert "ok=250" in text and "missing_seq=0" in text
    assert view.marks == [(1.0, "walk")]
    assert view.iq_heads.get_offsets().shape == (56, 2)  # IQ の先頭の点が 56 本分
    assert len(view.iq_trails[0].get_xdata()) == 100  # 直近 1 秒 = 100 パケットの軌跡
    assert all(len(line.get_xdata()) == 57 for line in view.radar_traces)  # 56 + 輪を閉じる 1


@pytest.mark.parametrize("argv", [
    ["--replay", "no-such-file.csv"],
    ["--replay", "x.csv", "--record", "a"],
    ["--port", "/dev/no-such-port"],
    ["--duration", "0"],
    ["--ratio-threshold", "0"],
])
def test_main_rejects_bad_arguments(argv: list[str]) -> None:
    """引数の誤りは何もせず終了コード 1."""
    assert live.main(argv) == 1


def test_baseline_turns_motion_into_ratio() -> None:
    """静止基準を登録すると、傾きの揺れ（歩行の模擬）が基準の何倍かで判定される."""
    rng = np.random.default_rng(0)
    buf, recorder = live.LiveBuffer(), live.Recorder(None)
    view = live.LiveView(buf, recorder, {}, "test", ratio_threshold=2.0)
    tilt = np.linspace(-1.0, 1.0, 64)  # 生の並びのまま（周波数順でなくても傾きの成分は残る）
    n_static, n_walk = 1200, 300  # 12 秒静止 → 3 秒歩行（100 Hz）
    for i in range(n_static + n_walk):
        noise = rng.integers(-1, 2, 64)  # ±1 の雑音
        swing = 6.0 * np.sin(2 * np.pi * i / 80) if i >= n_static else 0.0  # 0.8 秒周期
        amp = np.clip(np.round(40 + noise + swing * tilt), 1, 127).astype(np.int8)
        buf.append(recorder.handle(csi_line(i * 10_000, seq=i, amp=amp)))
        if i == n_static - 1:
            view.update()
            assert view.last_ratio is None  # 基準の登録前は倍率なし
            assert view.set_baseline()
            # 描画と基準の登録の間に MOTION_STALE_SEC を超えると倍率が出ないため、
            # 描く前に「今受けた」ことにする（遅い CI の機械で落ちた）
            buf.last_arrival = time.monotonic()
            view.update()
            assert view.last_ratio is not None and view.last_ratio < 2.0
    buf.last_arrival = time.monotonic()
    view.update()
    assert view.last_ratio is not None and view.last_ratio > 2.0
    assert "MOVING" in view.text.get_text()


def test_baseline_needs_enough_packets() -> None:
    """パケットが足りなければ基準を登録しない."""
    buf, recorder = live.LiveBuffer(), live.Recorder(None)
    view = live.LiveView(buf, recorder, {}, "test")
    assert not view.set_baseline()
    for i in range(50):
        buf.append(recorder.handle(csi_line(i * 10_000, seq=i)))
    assert not view.set_baseline()


def test_buffer_locks_onto_one_transmitter() -> None:
    """送信機 2 台の行が来ても、最初の送信機だけを表示し、もう 1 台は数えるだけ."""
    buf = live.LiveBuffer()
    s_line = csi_line(10_000, seq=1)
    rec = live.io.parse_rx_line(s_line.decode())
    other = live.io.RxLine(**{**rec.__dict__, "tx": "02:00:00:00:00:0c", "section": "lltf",
                              "raw": rec.raw[:128]})
    first = live.io.RxLine(**{**rec.__dict__, "tx": "02:00:00:00:00:0a"})
    buf.append(first)
    buf.append(other)
    assert buf.tx == "02:00:00:00:00:0a" and buf.other_tx == 1
    assert len(buf.snapshot().seq) == 1


def test_buffer_reads_lltf_transmitter_without_nan() -> None:
    """11g の送信機（LLTF だけ）は ltf="lltf" で読み、NaN になる +1 番を除いた 51 本にする."""
    raw = np.zeros((64, 2), dtype=np.int8)
    raw[:, 1] = AMP  # 実部
    body = (f"CSI,10000,02000000000c,5,-44,-96,11,0,0,0,0,0,0,1,lltf,128,{raw.tobytes().hex()}")
    line = f"{body}*{zlib.crc32(body.encode()):08x}".encode()
    buf = live.LiveBuffer(ltf="lltf")
    buf.append(live.Recorder(None).handle(line))
    snap = buf.snapshot()
    assert buf.subcarrier_index.size == 51 and 1 not in buf.subcarrier_index
    assert snap.amp.shape == (1, 51) and not np.isnan(snap.amp).any()
    view = live.LiveView(buf, live.Recorder(None), {}, "test")
    view.update()  # 51 本でも描ける


def test_link_status_per_transmitter() -> None:
    """表示していない送信機も含めて、送信機ごとのレート・RSSI・欠番を数える."""
    buf = live.LiveBuffer()
    base = live.io.parse_rx_line(csi_line(10_000, seq=0).decode())
    for i in range(20):
        if i != 7:  # 主: 連番 7 が欠け
            buf.append(live.io.RxLine(**{**base.__dict__, "seq": i, "rx_us": i * 10_000,
                                         "tx": "02:00:00:00:00:0a"}))
        buf.append(live.io.RxLine(**{**base.__dict__, "seq": 100 + i, "rssi_dbm": -40,
                                     "tx": "02:00:00:00:00:0c"}))
    status = {st.tx: st for st in buf.link_status()}
    s, p = status["02:00:00:00:00:0a"], status["02:00:00:00:00:0c"]
    assert (s.received, s.missing, p.received, p.missing) == (19, 1, 20, 0)
    assert p.rssi_dbm == -40 and p.rate_hz == 20 and s.silent_sec < 1
    view = live.LiveView(buf, live.Recorder(None), {}, "test")
    view.update()
    text = view.text.get_text()
    assert "*ESP32-DevKit (02000000000a)" in text and " PicoW (02000000000c)" in text  # 表示中は *


def test_read_loop_prefers_primary_from_header(tmp_path: Path) -> None:
    """見出しに主の送信機があれば、先に別の送信機の行が来ても主を表示する."""
    raw = np.zeros((64, 2), dtype=np.int8)
    raw[:, 1] = AMP
    rows = [b"# tx id=0 mac=02000000000a primary=1"]
    for i, tx in enumerate(("02000000000c", "02000000000a")):
        body = f"CSI,{i * 10_000},{tx},{i},-50,-96,11,0,1,0,0,0,0,0,htltf,128,{raw.tobytes().hex()}"
        rows.append(f"{body}*{zlib.crc32(body.encode()):08x}".encode())
    path = tmp_path / "rec.csv"
    path.write_bytes(b"\n".join(rows) + b"\n")
    buf = live.LiveBuffer()
    chunks = live.replay_chunks(path, threading.Event(), speed=0)
    live.read_loop(chunks, live.Recorder(None), buf, {})
    assert buf.tx == "02:00:00:00:00:0a" and buf.other_tx == 1


def two_tx_lines(n: int) -> list[bytes]:
    """見出し + 送信機 2 台（主 S が 1 本おき）の format=3 の行を作る."""
    raw = np.zeros((64, 2), dtype=np.int8)
    rows = [b"# tx id=0 mac=02000000000a primary=1"]
    rng = np.random.default_rng(0)
    for i in range(n):
        for tx in ("02000000000c", "02000000000a"):  # わざと 2 台目を先に流す
            raw[:, 1] = np.clip(AMP + rng.integers(-2, 3, 64), 1, 127)
            body = (f"CSI,{i * 10_000},{tx},{i},-50,-96,11,0,1,0,0,0,0,0,htltf,128,"
                    f"{raw.tobytes().hex()}")
            rows.append(f"{body}*{zlib.crc32(body.encode()):08x}".encode())
    return rows


def test_compare_assigns_each_transmitter_to_one_side(tmp_path: Path) -> None:
    """--compare: 左は見出しの主の送信機、右はもう 1 台に付き、両方描けて、基準も両方登録できる."""
    path = tmp_path / "rec.csv"
    path.write_bytes(b"\n".join(two_tx_lines(300)) + b"\n")
    left = live.LiveBuffer()
    right = live.LiveBuffer(peers=[left])
    left.peers = [right]
    live.read_loop(live.replay_chunks(path, threading.Event(), speed=0), live.Recorder(None),
                   [left, right], {})
    assert left.tx == "02:00:00:00:00:0a" and right.tx == "02:00:00:00:00:0c"
    assert len(left.snapshot().seq) == len(right.snapshot().seq) == 300
    view = live.CompareView([left, right], live.Recorder(None), {}, "test")
    # 一気に読み込んだので、描く前に「今受けた」ことにする（受信中と同じ状態。倍率は
    # 最後の受信から MOTION_STALE_SEC を超えると出さないため、画面の組み立てが遅いと落ちる）
    for buf in (left, right):
        buf.last_arrival = time.monotonic()
    view.update()
    assert view.set_baseline()
    for buf in (left, right):
        buf.last_arrival = time.monotonic()
    view.update()
    view.add_mark(1.0, "walk")
    text = view.text.get_text()
    assert "*ESP32-DevKit (02000000000a)" in text and "*PicoW (02000000000c)" in text  # 両方表示中
    assert all(m.last_ratio is not None for m in view.motions)


def test_compare_rejects_wrong_number_of_buffers() -> None:
    """並べるバッファは 2 つ."""
    with pytest.raises(ValueError):
        live.CompareView([live.LiveBuffer()], live.Recorder(None), {}, "test")


def test_tx2_requires_compare() -> None:
    """--tx2 だけでは使えない."""
    assert live.main(["--replay", __file__, "--tx2", "02000000000c"]) == 1


def test_names_from_receiver_header_override_defaults() -> None:
    """受信機の見出しの name= が既定の機種名より優先される."""
    rec = live.Recorder(None)
    assert rec.tx_names["02:00:00:00:00:0c"] == "PicoW"
    rec.handle(b"# tx id=1 mac=02000000000c name=PicoW-lab")
    assert rec.tx_names["02:00:00:00:00:0c"] == "PicoW-lab"
    assert live.tx_label("02:00:00:00:00:0c", rec.tx_names) == "PicoW-lab (02000000000c)"
    assert live.tx_label("aa:bb:cc:dd:ee:ff", rec.tx_names) == "aabbccddeeff"


def test_channel_changes_are_recorded_shown_and_drawn() -> None:
    """チャンネルが変わると記録され、受信状態に今のチャンネルが出て、グラフに ch の文字が入る."""
    buf = live.LiveBuffer()
    base = live.io.parse_rx_line(csi_line(10_000, seq=0).decode())
    for i, ch in enumerate([11] * 150 + [6] * 150):
        buf.append(live.io.RxLine(**{**base.__dict__, "seq": i, "rx_us": i * 10_000,
                                     "channel": ch, "tx": "02:00:00:00:00:0a"}))
    assert [c for _, c in buf.channel_changes] == [11, 6]
    assert buf.channel_changes[1][0] == pytest.approx(1.5)  # 150 パケット目 = 1.5 秒
    assert buf.link_status()[0].channel == 6
    view = live.LiveView(buf, live.Recorder(None), {}, "test")
    view.update()
    labels = [t.get_text().strip() for t in view.ax_wf.texts]
    assert labels.count("ch11") == 1 and labels.count("ch6") == 1
    assert "ch6" in view.text.get_text()
    view.update()  # 2 回目は描き足さない
    assert [t.get_text().strip() for t in view.ax_wf.texts].count("ch6") == 1


def test_resample_to_grid_fills_packets_and_leaves_gaps_blank() -> None:
    """受信が途切れた区間の列は NaN、受信のある区間は最も近いパケットの値になる."""
    rate_hz = 100.0
    t = np.concatenate([np.arange(0.0, 3.0, 1 / rate_hz), np.arange(6.0, 10.0, 1 / rate_hz)])
    rows = np.stack([t, -t], axis=1)
    grid = live.resample_to_grid(t, rows, t_end=10.0, span_sec=10.0, n_cols=100,
                                 tolerance_sec=0.05)
    assert grid.shape == (100, 2)
    centers = np.arange(100) * 0.1 + 0.05
    gap = (centers > 3.0) & (centers < 5.95)  # 3.05 は最後のパケット 2.99 から 0.06 秒
    assert np.isnan(grid[gap]).all()
    np.testing.assert_allclose(grid[~gap, 0], centers[~gap], atol=0.05 + 1e-9)
    np.testing.assert_allclose(grid[~gap, 1], -grid[~gap, 0])


def test_resample_to_grid_before_data_and_empty() -> None:
    """データより前の列と、データが空のときは全て NaN."""
    t = np.array([9.0, 9.01, 9.02])
    grid = live.resample_to_grid(t, np.ones((3, 4)), t_end=10.0, span_sec=10.0, n_cols=10)
    assert np.isnan(grid[:9]).all()
    assert np.isnan(grid[9]).all()  # 列の中央 9.5 から 0.48 秒離れている
    empty = live.resample_to_grid(np.zeros(0), np.zeros((0, 4)), t_end=1.0, n_cols=5)
    assert empty.shape == (5, 4) and np.isnan(empty).all()


def test_resample_to_grid_rejects_bad_shapes() -> None:
    """形の合わない入力と列の数 0 は ValueError."""
    with pytest.raises(ValueError):
        live.resample_to_grid(np.zeros(3), np.zeros((2, 4)), t_end=1.0)
    with pytest.raises(ValueError):
        live.resample_to_grid(np.zeros(3), np.zeros((3, 4)), t_end=1.0, n_cols=0)


def test_reference_by_channel_uses_same_channel_rows() -> None:
    """行ごとの基準は同じチャンネルの平均。チャンネルの違いが「ずれ」に残らない."""
    ch11 = np.tile([1.0, 2.0], (4, 1))
    ch6 = np.tile([3.0, 0.5], (3, 1))
    rows = np.concatenate([ch11, ch6, ch11])
    channels = np.array([11] * 4 + [6] * 3 + [11] * 4)
    ref = live.reference_by_channel(rows, channels)
    np.testing.assert_allclose(rows - ref, 0.0)


def test_reference_by_channel_prefers_baseline_of_same_channel() -> None:
    """静止基準にそのチャンネルがあればそれを、なければ直近の同じチャンネルの平均を使う."""
    rows = np.array([[1.0, 1.0], [5.0, 5.0], [7.0, 7.0]])
    channels = np.array([11, 6, 6])
    base = np.array([[0.0, 2.0], [0.0, 4.0]])
    ref = live.reference_by_channel(rows, channels, base, np.array([11, 11]))
    np.testing.assert_allclose(ref, [[0.0, 3.0], [6.0, 6.0], [6.0, 6.0]])


def test_reference_by_channel_rejects_bad_input() -> None:
    """数の合わない入力と、基準の片方だけの指定は ValueError."""
    with pytest.raises(ValueError):
        live.reference_by_channel(np.zeros((3, 2)), np.zeros(2, dtype=np.int64))
    with pytest.raises(ValueError):
        live.reference_by_channel(np.zeros((3, 2)), np.zeros(3, dtype=np.int64), np.zeros((1, 2)))
    with pytest.raises(ValueError):
        live.reference_by_channel(np.zeros((3, 2)), np.zeros(3, dtype=np.int64),
                                  np.zeros((2, 2)), np.zeros(1, dtype=np.int64))


def _static_rows(shape: np.ndarray, n: int, rng: np.random.Generator) -> np.ndarray:
    """形 ``shape`` に小さな揺らぎを乗せた静止の行 (n, 本数)."""
    return shape + rng.normal(0.0, 0.01, (n, shape.size))


def test_motion_tracker_ignores_channel_switch() -> None:
    """チャンネルを行き来しても静止なら倍率は小さい。切り替え直後と基準の無いチャンネルは待つ."""
    rng = np.random.default_rng(0)
    w = live.MOTION_WINDOW_PACKETS
    shape11 = np.linspace(0.5, 1.5, 8)
    shape6 = shape11[::-1].copy()  # ch6 は形がまるで違う
    motion = live.MotionTracker()
    base = _static_rows(shape11, 3 * w, rng)
    t = np.arange(len(base)) / live.PACKET_RATE_HZ
    assert motion.set_baseline(t, base, np.full(len(base), 11), float(t[-1]))

    rows = np.concatenate([base, _static_rows(shape6, w // 2, rng)])
    channel = np.array([11] * len(base) + [6] * (w // 2))
    motion.update(rows, channel)
    assert motion.last_ratio is None and "settling" in motion.describe(2.0)

    rows = np.concatenate([rows, _static_rows(shape6, w, rng)])
    channel = np.concatenate([channel, np.full(w, 6)])
    motion.update(rows, channel)
    assert motion.last_ratio is None and "no baseline on ch6" in motion.describe(2.0)

    rows = np.concatenate([rows, _static_rows(shape11, w, rng)])
    channel = np.concatenate([channel, np.full(w, 11)])
    motion.update(rows, channel)
    assert motion.last_ratio is not None and motion.last_ratio < 2.0


def test_motion_tracker_update_rejects_mismatch() -> None:
    """行とチャンネルの数が合わなければ ValueError."""
    with pytest.raises(ValueError):
        live.MotionTracker().update(np.zeros((3, 2)), np.zeros(2, dtype=np.int64))


def test_motion_tracker_reports_no_reception_when_silent() -> None:
    """受信が止まっていれば、古い窓の倍率は出さずに「no reception」と出す."""
    rng = np.random.default_rng(1)
    w = live.MOTION_WINDOW_PACKETS
    rows = _static_rows(np.linspace(0.5, 1.5, 8), 3 * w, rng)
    channel = np.full(len(rows), 11)
    t = np.arange(len(rows)) / live.PACKET_RATE_HZ
    motion = live.MotionTracker()
    assert motion.set_baseline(t, rows, channel, float(t[-1]))
    motion.update(rows, channel, silent_sec=0.1)
    assert motion.last_ratio is not None
    motion.update(rows, channel, silent_sec=live.MOTION_STALE_SEC + 1.0)
    assert motion.last_ratio is None and "no reception" in motion.describe(2.0)


def test_live_buffer_silent_sec() -> None:
    """最後に受けてからの時間。まだ受けていなければ None."""
    buffer = live.LiveBuffer()
    assert buffer.silent_sec() is None
    buffer.append(live.io.parse_rx_line(csi_line(1000).decode()))
    assert buffer.last_arrival is not None
    assert buffer.silent_sec(now=buffer.last_arrival + 2.5) == pytest.approx(2.5)


class _FakeSerial:
    """書いた命令を覚え、決めておいた応答を読ませるシリアルの代わり."""

    def __init__(self, replies: list[bytes] | None = None) -> None:
        """応答（読むたびに先頭から 1 つずつ返す）を受け取る."""
        self.written: list[bytes] = []
        self.replies = list(replies or [])

    def write(self, data: bytes) -> int:
        """書いた内容を覚える."""
        self.written.append(data)
        return len(data)

    def read(self, size: int) -> bytes:
        """応答を 1 つ返す（無ければ空）."""
        return self.replies.pop(0) if self.replies else b""


def test_channel_hopper_key_cycle_ack_and_busy() -> None:
    """「c」キーは今のチャンネルの次を送り、応答待ちの間は送らず、ack で今のチャンネルが変わる."""
    ser = _FakeSerial()
    hopper = live.ChannelHopper(ser.write, [11, 6, 1])
    hopper.observe(11, now=0.0)
    assert hopper.request(now=1.0)
    assert ser.written == [b"CH 6\n"] and hopper.pending == 6
    assert not hopper.request(now=1.1)  # 応答待ち
    assert hopper.on_line("# ack cmd=CH channel=6 relay=ok acked=1 attempts=1", now=1.5)
    assert hopper.current == 6 and hopper.pending is None and hopper.switches == 1
    assert hopper.request(now=2.0) and ser.written[-1] == b"CH 1\n"
    hopper.on_line("# ack cmd=CH channel=1 relay=ok acked=1 attempts=1", now=2.5)
    assert hopper.next_channel() == 11  # 一周して先頭へ
    assert not hopper.on_line("# stats ok=1")


def test_channel_hopper_err_and_timeout() -> None:
    """応答の err と時間切れは失敗として出し、今のチャンネルは変えない."""
    ser = _FakeSerial()
    hopper = live.ChannelHopper(ser.write, [11, 6], reply_timeout_sec=4.0)
    hopper.observe(11, now=0.0)
    hopper.request(now=1.0)
    hopper.on_line("# err cmd=CH reason=tx_no_ack channel=6 attempts=3", now=2.0)
    assert hopper.current == 11 and hopper.pending is None
    assert "tx_no_ack" in hopper.describe(now=2.0)
    hopper.request(now=3.0)
    hopper.tick(now=7.5)
    assert hopper.pending is None and "no reply" in hopper.last_error
    hopper.request(now=8.0)
    hopper.on_line("# ack cmd=CH channel=6 relay=ok acked=1 attempts=1", now=8.5)
    assert hopper.last_error == ""


def test_channel_hopper_auto_hop_after_dwell() -> None:
    """--hop は切り替えが済んでから dwell 秒たつと次を送る."""
    ser = _FakeSerial()
    hopper = live.ChannelHopper(ser.write, [11, 6], dwell_sec=3.0)
    hopper.tick(now=10.0)
    assert ser.written == []  # まだ受信していない（今のチャンネルが分からない）
    hopper.observe(11, now=10.0)
    hopper.tick(now=12.9)
    assert ser.written == []
    hopper.tick(now=13.0)
    assert ser.written == [b"CH 6\n"]
    hopper.on_line("# ack cmd=CH channel=6 relay=ok acked=1 attempts=1", now=14.0)
    assert "next in 3 s" in hopper.describe(now=14.0)
    hopper.tick(now=16.9)
    assert len(ser.written) == 1
    hopper.tick(now=17.0)
    assert ser.written[-1] == b"CH 11\n"


def test_channel_hopper_validation_and_replay() -> None:
    """範囲外・重複・短すぎる間隔は ValueError。再生中（書き先なし）は送らない."""
    with pytest.raises(ValueError):
        live.ChannelHopper(None, [])
    with pytest.raises(ValueError):
        live.ChannelHopper(None, [11, 14])
    with pytest.raises(ValueError):
        live.ChannelHopper(None, [11, 11])
    with pytest.raises(ValueError):
        live.ChannelHopper(None, [11, 6], dwell_sec=live.MIN_DWELL_SEC - 0.5)
    hopper = live.ChannelHopper(None)
    assert not hopper.enabled and not hopper.request()
    assert live.channel_key_hint(hopper) == ""
    assert "c=next channel (11,6,1)" in live.channel_key_hint(live.ChannelHopper(print))


def test_restore_channel_returns_to_start() -> None:
    """起動時と違うチャンネルなら CH を送って戻す。同じなら何も送らない."""
    ser = _FakeSerial([b"CSI,garbage\n# ack cmd=CH channel=11 relay=ok acked=1 attempts=1\n"])
    hopper = live.ChannelHopper(ser.write, [11, 6])
    hopper.observe(11, now=0.0)
    hopper.on_line("# ack cmd=CH channel=6 relay=ok acked=1 attempts=1", now=1.0)
    live.restore_channel(ser, hopper)  # type: ignore[arg-type]
    assert ser.written == [b"CH 11\n"] and hopper.current == 11
    live.restore_channel(ser, hopper)  # type: ignore[arg-type]
    assert ser.written == [b"CH 11\n"]


def test_read_loop_passes_channel_replies_to_hopper() -> None:
    """受信スレッドは CH の応答と、受けた CSI のチャンネルを切り替え役に渡す."""
    hopper = live.ChannelHopper(_FakeSerial().write, [11, 6])
    chunks = iter([csi_line(1000) + b"\n", b"# ack cmd=CH channel=6 relay=ok acked=1\n"])
    live.read_loop(chunks, live.Recorder(None), live.LiveBuffer(), {}, hopper=hopper)
    assert hopper.start_channel == 11 and hopper.current == 6


def test_main_rejects_bad_hop(tmp_path: Path) -> None:
    """--hop は再生と一緒に使えず、数字でない・1 つだけの指定も断る."""
    replay = tmp_path / "r.csv"
    replay.write_text("", encoding="utf-8")
    assert live.main(["--replay", str(replay), "--hop", "11,6"]) == 1
    assert live.main(["--port", str(replay), "--hop", "11,x"]) == 1
    assert live.main(["--port", str(replay), "--hop", "11"]) == 1


def test_hopper_switches_follower_together_and_tracks_its_channel() -> None:
    """R と同時に Pico W にも CH を送り、Pico W の ack・WHO・毎秒の報告でチャンネルを知る."""
    rx, pico = _FakeSerial(), _FakeSerial()
    follower = live.Follower("PicoW", pico.write)
    hopper = live.ChannelHopper(rx.write, [11, 6], followers=[follower])
    who = "[picow] who role=tx-picow mac=02000000000c channel=11"
    assert hopper.on_follower_line(follower, who)
    assert follower.channel == 11 and follower.start_channel == 11
    hopper.observe(11, now=0.0)
    assert hopper.request(now=1.0)
    assert rx.written == [b"CH 6\n"] and pico.written == [b"CH 6\n"]
    assert hopper.on_follower_line(follower, "[picow] ack cmd=CH channel=6 elapsed_ms=45 mrate=0x0")
    hopper.on_line("# ack cmd=CH channel=6 relay=ok acked=1 attempts=1", now=1.3)
    assert follower.channel == 6 and "PicoW: ch6" in hopper.describe(now=1.3)
    report = "[picow] seq=100 rate_hz_x10=1000 send_err=0 channel=6"
    assert hopper.on_follower_line(follower, report)
    assert not hopper.on_follower_line(follower, "[picow] sending")
    hopper.tick(now=10.0)
    assert pico.written == [b"CH 6\n"]  # 揃っていれば送り直さない


def test_hopper_resyncs_follower_when_rx_switch_fails() -> None:
    """R の切り替えが失敗して元に戻ったら、先に切り替わった Pico W を R のチャンネルに戻す."""
    rx, pico = _FakeSerial(), _FakeSerial()
    follower = live.Follower("PicoW", pico.write, channel=11, start_channel=11)
    hopper = live.ChannelHopper(rx.write, [11, 6], reply_timeout_sec=4.0, followers=[follower])
    hopper.observe(11, now=0.0)
    hopper.request(now=1.0)
    hopper.on_follower_line(follower, "[picow] ack cmd=CH channel=6 elapsed_ms=45 mrate=0x0")
    hopper.on_line("# err cmd=CH reason=no_link channel=6 back_to=11", now=2.0)
    hopper.tick(now=3.0)
    assert pico.written == [b"CH 6\n"]  # 直前に送ったばかりなので待つ
    hopper.tick(now=5.5)
    assert pico.written[-1] == b"CH 11\n"
    hopper.on_follower_line(follower, "[picow] err cmd=CH reason=ioctl channel=11 down=0")
    assert "ioctl" in hopper.describe(now=5.6)


def test_restore_follower_returns_to_start() -> None:
    """Pico W が起動時と違うチャンネルなら CH を送って戻す。同じなら何も送らない."""
    pico = _FakeSerial([b"[picow] seq=1 channel=6\n[picow] ack cmd=CH channel=11 elapsed_ms=44\n"])
    follower = live.Follower("PicoW", pico.write, channel=6, start_channel=11)
    hopper = live.ChannelHopper(_FakeSerial().write, [11, 6], followers=[follower])
    live.restore_follower(pico, hopper, follower)  # type: ignore[arg-type]
    assert pico.written == [b"CH 11\n"] and follower.channel == 11
    live.restore_follower(pico, hopper, follower)  # type: ignore[arg-type]
    assert pico.written == [b"CH 11\n"]


def test_open_picow_checks_who(monkeypatch: pytest.MonkeyPatch) -> None:
    """WHO に Pico W として応答すれば開いたまま返し、応答しなければ閉じて None."""

    class Port(_FakeSerial):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(list(replies))
            self.closed = False

        def reset_input_buffer(self) -> None:
            """読み残しを捨てる（何もしない）."""

        def close(self) -> None:
            """閉じた印を付ける."""
            self.closed = True

    replies = [b"[picow] who role=tx-picow mac=02000000000c channel=11\n"]
    monkeypatch.setattr(live.serial, "Serial", Port)
    ser = live.open_picow("/dev/fake")
    assert ser is not None and ser.written == [b"WHO\n"] and not ser.closed
    replies = [b"[picow] err cmd=WHO reason=unknown\n"]
    assert live.open_picow("/dev/fake", timeout_sec=0.2) is None


def test_follower_loop_passes_lines_until_stopped() -> None:
    """追従する送信機の行を読んで渡し、止める合図で終わる."""
    stop = threading.Event()
    pico = _FakeSerial([b"[picow] ack cmd=CH channel=6 elapsed_ms=45\n"])
    follower = live.Follower("PicoW", pico.write)
    hopper = live.ChannelHopper(_FakeSerial().write, followers=[follower])
    th = threading.Thread(target=live.follower_loop, args=(pico, hopper, follower, stop))
    th.start()
    deadline = time.monotonic() + 2.0
    while follower.channel is None and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    th.join(timeout=2.0)
    assert follower.channel == 6 and not th.is_alive()


def test_hopper_reads_pico_relay_status_from_rx() -> None:
    """R の CH の応答の pico= と、接続の出来事（joined / lost）を画面に出す."""
    hopper = live.ChannelHopper(_FakeSerial().write, [11, 6])
    hopper.observe(11, now=0.0)
    assert hopper.on_line("# pico joined ms=52 channel=11") is False  # CH の応答ではない
    assert "PicoW(radio): joined" in hopper.describe(now=0.0)
    hopper.request(now=1.0)
    hopper.on_line("# ack cmd=CH channel=6 relay=ok acked=1 attempts=1 pico=ok", now=1.4)
    assert hopper.current == 6 and "PicoW(radio): ok" in hopper.describe(now=1.4)
    hopper.request(now=5.0)
    hopper.on_line("# ack cmd=CH channel=11 relay=ok acked=1 attempts=1 pico=no_ack", now=5.4)
    assert hopper.current == 11 and "PicoW(radio): !! no_ack" in hopper.describe(now=5.4)
    hopper.on_line("# err cmd=CH reason=no_link channel=6 back_to=11 pico=recalled", now=9.0)
    assert "!! recalled" in hopper.describe(now=9.0)
    hopper.on_line("# pico lost channel=11")
    assert "!! lost" in hopper.describe()


def test_hopper_without_pico_relay_shows_nothing() -> None:
    """古い R（pico= なし）では Pico W の欄を出さない."""
    hopper = live.ChannelHopper(_FakeSerial().write, [11, 6])
    hopper.observe(11, now=0.0)
    hopper.request(now=1.0)
    hopper.on_line("# ack cmd=CH channel=6 relay=ok acked=1 attempts=1", now=1.4)
    assert "PicoW(radio)" not in hopper.describe(now=1.4)
