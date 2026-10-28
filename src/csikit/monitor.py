"""軽い受信のモニタ（csikit monitor）. 受信機をリセットせずに開き、受信の様子を画面に出す.

画面:

- 上: 振幅のヒートマップ（横 = 直近 ``WINDOW_SEC`` 秒、縦 = サブキャリア）。手を振ると模様が乱れる
- 下: RSSI の推移 [dBm]
- 題名: 直近 1 秒の受信レート [Hz]・連番から数えた欠け [%]・受信機内の取りこぼし
  （``# stats`` の dropped）

記録はしない（記録は csikit record）。組んだ送信機（``WHO`` の ``tx_mac``）の行だけを使う。

失敗時の挙動: ポートを開けない・``WHO`` に答えない・組が未設定なら MonitorError（理由と直し方）。
表示の途中で行が壊れていたら（CRC 不一致）数えて捨てる。受信が止まれば題名に「受信なし」と出す。
"""

from __future__ import annotations

import queue
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.typing import NDArray

from csikit import io, ports

WINDOW_SEC = 10.0  # ヒートマップに出す長さ [sec]
RATE_WINDOW_SEC = 1.0  # 受信レート・欠けを数える長さ [sec]
NOMINAL_RATE_HZ = 100  # 送信機の送信レート [Hz]（firmware/csi-tx の SEND_INTERVAL_MS = 10）
MAX_PACKETS = int(WINDOW_SEC * NOMINAL_RATE_HZ * 1.5)  # 窓に持つパケットの上限（余裕を見て 1.5 倍）
UPDATE_MS = 200  # 画面を書き直す間隔 [ms]
LTF: io.Ltf = "htltf"  # 出す区間（送信機は 11n なので HT-LTF の 56 本）
SILENT_SEC = 2.0  # これだけ受信が無ければ「受信なし」[sec]
TX_LABEL = "T1"  # 送信機の機器番号（ファームの CSI_LABEL と同じ。図には MAC の代わりにこれを出す）
RX_LABEL = "R1"  # 受信機の機器番号
_DROPPED = re.compile(r"\bdropped=(\d+)\b")  # 受信機の # stats の取りこぼし


class MonitorError(RuntimeError):
    """モニタを始められない（理由と直し方）."""


@dataclass
class MonitorState:
    """受信の窓と集計（画面と切り離してテストする）."""

    tx_mac: str | None = None  # 組んだ送信機（コロン区切り）。None なら最初に来た送信機
    times: deque[float] = field(default_factory=lambda: deque(maxlen=MAX_PACKETS))
    seqs: deque[int] = field(default_factory=lambda: deque(maxlen=MAX_PACKETS))
    rssi: deque[int] = field(default_factory=lambda: deque(maxlen=MAX_PACKETS))
    amps: deque[NDArray[np.float64]] = field(default_factory=lambda: deque(maxlen=MAX_PACKETS))
    bad_crc: int = 0
    other_tx: int = 0
    dropped: int | None = None  # 受信機の # stats の dropped（起動からの累計）

    def feed(self, text: str, now: float) -> None:
        """受信機の 1 行を取り込む.

        Args:
            text: 改行を除いた 1 行。
            now: 受け取った時刻 [sec]（単調な時計）。
        """
        if text.startswith(io.RX_COMMENT_PREFIX):
            if text.startswith("# stats") and (m := _DROPPED.search(text)):
                self.dropped = int(m.group(1))
            return
        if not text.startswith(io.RX_LINE_PREFIX):
            return
        try:
            rec = io.parse_rx_line(text)
        except io.RxCrcError:
            self.bad_crc += 1
            return
        except ValueError:
            return
        if self.tx_mac is None:
            self.tx_mac = rec.tx
        if rec.tx != self.tx_mac:
            self.other_tx += 1
            return
        cap = io.lines_to_capture([rec], ltf=LTF)
        self.times.append(now)
        self.seqs.append(rec.seq)
        self.rssi.append(rec.rssi_dbm)
        self.amps.append(np.abs(cap.csi[0, :, 0]))

    def recent(self, now: float, span_sec: float) -> int:
        """直近 ``span_sec`` 秒に入ったパケットの数."""
        return sum(1 for t in self.times if now - t <= span_sec)

    def rate_hz(self, now: float) -> float:
        """直近 1 秒の受信レート [Hz]."""
        return self.recent(now, RATE_WINDOW_SEC) / RATE_WINDOW_SEC

    def loss_pct(self, now: float) -> float | None:
        """直近 1 秒の連番の欠け [%]（連番が分からなければ None）."""
        n = self.recent(now, RATE_WINDOW_SEC)
        seq = np.array(list(self.seqs)[-n:], dtype=np.int64) if n else np.array([], np.int64)
        if seq.size < 2:
            return None
        missing = io.count_missing_by_seq(seq)
        if missing is None:
            return None
        return 100.0 * missing / (seq.size + missing)

    def silent(self, now: float) -> bool:
        """受信が止まっているか."""
        return not self.times or now - self.times[-1] > SILENT_SEC

    def heatmap(self) -> NDArray[np.float64]:
        """振幅の窓（形状 (サブキャリア, パケット)）。空なら (サブキャリア, 0)."""
        n_sub = len(io.valid_subcarrier_index(LTF))
        if not self.amps:
            return np.zeros((n_sub, 0))
        return np.stack(list(self.amps), axis=1)

    def title(self, now: float) -> str:
        """題名の文（ASCII と数値だけ。フォントが無くても読めるように）.

        MAC は出さない（保存した図・画面の共有から実機の MAC が漏れないように。8 章）。
        送信機は機器番号（``TX_LABEL``）で示す。MAC は ``csikit doctor`` の表示で確かめる。
        """
        if self.silent(now):
            return f"NO SIGNAL from {TX_LABEL}  (bad_crc={self.bad_crc})"
        loss = self.loss_pct(now)
        loss_text = "-" if loss is None else f"{loss:.1f}%"
        rssi = list(self.rssi)[-1]
        dropped = "-" if self.dropped is None else str(self.dropped)
        return (f"{self.rate_hz(now):.0f} Hz   loss {loss_text}   RSSI {rssi} dBm   "
                f"dropped {dropped}   bad_crc {self.bad_crc}")



def start_reader(port: ports.SerialPort, out: queue.Queue[tuple[str, float]],
                 stop: threading.Event,
                 clock: Callable[[], float] = time.monotonic) -> threading.Thread:
    """ポートを読み続けるスレッドを始める（画面の更新を待たせないため）."""
    def run() -> None:
        while not stop.is_set():
            try:
                line = ports.read_line(port)
            except OSError:
                out.put(("# reader stopped", clock()))
                return
            if line:
                out.put((line, clock()))

    thread = threading.Thread(target=run, name="csikit-monitor-reader", daemon=True)
    thread.start()
    return thread


def drain(q: queue.Queue[tuple[str, float]], state: MonitorState) -> int:
    """届いている行をすべて取り込む. 取り込んだ行の数を返す."""
    n = 0
    while True:
        try:
            text, at = q.get_nowait()
        except queue.Empty:
            return n
        state.feed(text, at)
        n += 1


def open_receiver(port: str, opener: ports.Opener = ports.open_no_reset
                  ) -> tuple[ports.SerialPort, ports.Board]:
    """受信機をリセットせずに開き、WHO で確かめる.

    Raises:
        MonitorError: 開けない・答えない・送信機でない・組が未設定。
    """
    try:
        s = opener(port, ports.RX_BAUD)
    except OSError as e:
        raise MonitorError(f"{e}（他のアプリがポートを使っていないか確認）") from e
    ports.flush_partial_line(s)
    board = ports.ask_who(s, port, ports.RX_BAUD)
    if board is None or board.role != "rx":
        s.close()
        found = "答えない" if board is None else f"{board.role} が答えた"
        raise MonitorError(f"{port} の受信機が確かめられません（{found}）。"
                           "uv run csikit doctor で確認")
    if board.paired is False:
        s.close()
        raise MonitorError("受信機の組が未設定です。uv run csikit flash --pair-only で組ませる")
    return s, board


def build_view(state: MonitorState, title: str = "csikit monitor"
               ) -> tuple[Any, Callable[[float], list[Any]]]:
    """図と、それを state の今の中身で描き直す関数を作る.

    Returns:
        (図, 描き直す関数（引数は今の時刻 [sec]。描き直した部品を返す）)。
    """
    import matplotlib.pyplot as plt

    fig, (ax_map, ax_rssi) = plt.subplots(2, 1, figsize=(9, 6), height_ratios=(3, 1))
    n_sub = len(io.valid_subcarrier_index(LTF))
    image = ax_map.imshow(np.zeros((n_sub, 1)), aspect="auto", origin="lower",
                          extent=(-WINDOW_SEC, 0, 0, n_sub), cmap="viridis")
    ax_map.set_xlim(-WINDOW_SEC, 0)  # 下の RSSI と同じ幅（窓より古い行は描かない）
    ax_map.set_ylabel("subcarrier")
    fig.colorbar(image, ax=ax_map, label="|CSI|")
    (rssi_line,) = ax_rssi.plot([], [])
    ax_rssi.set_xlim(-WINDOW_SEC, 0)
    ax_rssi.set_xlabel("time [s]")
    ax_rssi.set_ylabel("RSSI [dBm]")
    if fig.canvas.manager is not None:
        fig.canvas.manager.set_window_title(title)

    def redraw(now: float) -> list[Any]:
        heat = state.heatmap()
        if heat.shape[1]:
            image.set_data(heat)
            low, high = np.nanpercentile(heat, 1), np.nanpercentile(heat, 99)
            image.set_clim(low, max(high, low + 1e-9))  # 全部同じ値でも描けるように
            t = np.array(state.times) - now
            image.set_extent((float(t[0]), 0, 0, n_sub))
            rssi = np.array(state.rssi)
            rssi_line.set_data(t, rssi)
            ax_rssi.set_ylim(rssi.min() - 3, rssi.max() + 3)
        ax_map.set_title(state.title(now))
        return [image, rssi_line]

    return fig, redraw


def run(port: str, duration_sec: float | None = None,
        save_figure: str | None = None) -> int:  # pragma: no cover  画面を出すので手で確かめる
    """モニタを出す（閉じるか duration_sec が過ぎるまで）."""
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation

    from csikit import plotting

    plotting.use_japanese_font()
    s, board = open_receiver(port)
    state = MonitorState(tx_mac=io.colon_mac(board.peer_mac))
    q: queue.Queue[tuple[str, float]] = queue.Queue()
    stop = threading.Event()
    start_reader(s, q, stop)
    started = time.monotonic()
    fig, redraw = build_view(state, f"csikit monitor  {RX_LABEL}={port}")  # MAC は出さない

    def update(_: int) -> list[Any]:
        drain(q, state)
        now = time.monotonic()
        artists = redraw(now)
        if duration_sec is not None and now - started >= duration_sec:
            plt.close(fig)
        return artists  # 古い matplotlib（Python 3.10）の型は部品を返す関数を求める

    anim = FuncAnimation(fig, update, interval=UPDATE_MS, cache_frame_data=False)
    try:
        plt.show()
        if save_figure:
            redraw(time.monotonic())
            fig.savefig(save_figure)
    finally:
        del anim
        stop.set()
        s.close()
    return 0
