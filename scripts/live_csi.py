"""受信機の CSI をリアルタイムで可視化する（任意で同時に記録する）.

    uv run python scripts/live_csi.py                              # 見るだけ
    uv run python scripts/live_csi.py --record exp04-run1          # 見ながら data/raw/ に記録
    uv run python scripts/live_csi.py --replay data/raw/<file>.csv # 記録を再生（実機なし）
    uv run python scripts/live_csi.py --compare                    # 送信機 2 台を左右に並べる

画面（2×2）:
- 左上: 正規化振幅のウォーターフォール（縦 = サブキャリア、横 = 時間、直近 10 秒）
- 右上: 基準からのずれ（赤 = 増えた / 青 = 減った / 白 = 変化なし）。基準は b キーの静止基準の平均
  （未登録なら直近 10 秒の平均）。色の範囲は固定
- 左下: 56 サブキャリアの IQ 平面の軌跡（直近 1 秒。位相はサブキャリア方向の直線成分を除去）
- 右下: 円形レーダー（56 サブキャリアを円周に並べ、正規化振幅を半径に。直近 1 秒を重ね描き）
- 上部: 動きの判定（PC1 の 1 秒標準偏差 ÷ 静止基準。しきい値を超えると赤）と受信状態

キー（時間軸のあるグラフに縦線。記録中はファイルに ``# mark ...`` 行を書く）:
    b = 直近 10 秒を静止基準にする（誰も動いていない間に押す）
    1 = static / 2 = walk / 3 = empty / 0 = mark（その他）
    （q は matplotlib の既定で「ウィンドウを閉じる」）

主軸は毎回求め直す（前回と向きを揃える）。倍率は、基準区間を今の主軸に投影し直して計算するので、
主軸が変わっても意味がずれない。

シリアルポートは 1 つのプロセスしか開けない。記録しながら見るときは ``capture_csi.py`` ではなく
``--record`` を使う。記録の形式は ``capture_csi.py`` と同じで、``io.load_rx_csv`` で読める。
記録した場合は、終了時にメタデータの下書き ``data/meta/<記録名>.toml`` を書く
（``capture_csi.py`` と同じ。印は区間・出来事として下書きに入る）。

失敗時の挙動:
- ポートが無い・ラベルが不正・同名ファイルがある → 何も書かずに終了（終了コード 1）
- 受信が ``NO_DATA_WARN_SEC`` 秒途切れる → 画面に警告（送信機の電源・チャンネルを確認）
- シリアルが切れた → 画面に表示し、記録はそこまでで閉じる
- ウィンドウを閉じる / Ctrl-C / ``--duration`` 経過 → 記録に集計を追記して終了
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

import matplotlib.pyplot as plt
import numpy as np
import serial
from matplotlib.animation import FuncAnimation
from numpy.typing import NDArray

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))  # 同じディレクトリの capture_csi を使う

import capture_csi as cap  # noqa: E402

from csikit import features, io, plotting, ports, preprocess  # noqa: E402

PACKET_RATE_HZ = 100  # 送信レート（firmware/csi-tx）
MOTION_WINDOW_PACKETS = PACKET_RATE_HZ  # 動きの指標の窓（1 秒。実験01 と同じ）
WATERFALL_SEC = 10.0  # ウォーターフォールに出す長さ [sec]
HISTORY_SEC = 60.0  # 動きの指標・RSSI に出す長さ [sec]（バッファの長さも兼ねる）
BUFFER_PACKETS = int(HISTORY_SEC * PACKET_RATE_HZ * 1.2)  # 余裕を 2 割持たせる
COMPARE_LINKS = 2  # --compare で並べるリンクの数
CHANNEL_LINE_COLOR = "magenta"  # チャンネルが変わった所の縦線（印の色と重ならない色）
WATERFALL_COLUMNS = 500  # ウォーターフォールの列の数（直近 WATERFALL_SEC 秒を等分。1 列 20 ms）
GAP_TOLERANCE_SEC = 0.05  # 列の時刻からこれより離れたパケットしかなければ「途切れ」として空白にする
# 送信機の機種名の既定（受信機の見出し "# tx id=… mac=… name=…" が来ればそちらを使う。
# --no-reset では見出しが出ないので、その時のための表。MAC は firmware の README と同じ）
KNOWN_TX_NAMES = {"02:00:00:00:00:0a": "ESP32-DevKit", "02:00:00:00:00:0c": "PicoW"}
STATUS_WINDOW_SEC = 10.0  # 送信機ごとの欠番を数える長さ [sec]
RATE_WINDOW_SEC = 1.0  # 受信レートを数える長さ [sec]
UPDATE_INTERVAL_MS = 200  # 画面の更新間隔 [ms]
NO_DATA_WARN_SEC = 2.0  # 受信がこれ以上途切れたら警告 [sec]
MOTION_STALE_SEC = 0.3  # 最後の受信からこれ以上たったら倍率を出さない [sec]（30 パケット分）
CLIM_PERCENTILES = (1, 99)  # ウォーターフォールの色の範囲（外れ値で潰れないように）
Y_HEADROOM = 1.1  # 縦軸の余白（最大値の倍率）
BASELINE_SEC = 10.0  # b キーで静止基準にする直近の長さ [sec]
MIN_BASELINE_PACKETS = 2 * MOTION_WINDOW_PACKETS  # 基準に要る最小パケット数（1 秒窓 2 つ）
MIN_BASELINE_STD = 1e-6  # 基準の標準偏差の下限（0 除算の回避。正規化振幅の単位）
DEFAULT_RATIO_THRESHOLD = 2.0  # 「動きあり」とみなす倍率（実験01 の PC1 は中央値 4.8 倍）
DEVIATION_CLIM = 0.3  # 「基準からのずれ」の色の範囲（正規化振幅、±）。固定して雑音を強調しない
TRAIL_SEC = 1.0  # IQ 軌跡・レーダーに重ねる長さ [sec]
RADAR_TRACES = 25  # レーダーに重ねる線の数（直近 TRAIL_SEC 秒から間引く）
RADAR_RLIM = (0.4, 1.6)  # レーダーの半径の範囲（正規化振幅）
RADAR_TICK_STEP = 8  # レーダーの目盛りを付けるサブキャリアの間隔
IQ_MIN_HALF_WIDTH = 0.5  # IQ 平面の表示の最小半幅（正規化振幅）。静止時の雑音を拡大しない
BASELINE_KEY = "b"
LLTF_INVALID_INDEX = 1  # LLTF で first_word_invalid のとき NaN になる番号（0 番は元から空）
MARK_KEYS = {"1": "static", "2": "walk", "3": "empty", "0": "mark"}
CHANNEL_KEY = "c"  # 次のチャンネルへ切り替えるキー
DEFAULT_KEY_CHANNELS = (11, 6, 1)  # --hop が無いとき c キーで回る順（2.4 GHz の重ならない 3 つ）
MIN_WIFI_CHANNEL = 1  # 切り替えられるチャンネルの範囲（日本の 11n: ch1〜13。ch14 は DSSS のみ）
MAX_WIFI_CHANNEL = 13
DEFAULT_DWELL_SEC = 3.0  # --hop で 1 つのチャンネルにいる時間 [sec]
MIN_DWELL_SEC = 2.0  # 滞在の下限 [sec]（中継の確認に最悪 2 秒余り、倍率の窓に 1 秒要る）
CH_REPLY_TIMEOUT_SEC = 4.0  # CH の応答を待つ時間 [sec]（sweep_channels の RELAY_REPLY_TIMEOUT_SEC）
CH_ACK_PATTERN = re.compile(r"#\s*ack cmd=CH channel=(\d+)")
CH_ERR_PATTERN = re.compile(r"#\s*err cmd=CH reason=(\w+) channel=(\d+)")
# R が無線で切り替える Pico W の結果（CH の ack / err の pico=）と、接続の出来事（2026-09-23 から）
PICO_RELAY_PATTERN = re.compile(r"\bpico=(\w+)")
PICO_EVENT_PATTERN = re.compile(r"#\s*pico (joined|lost|join_failed)")
PICO_RELAY_OK = {"ok", "joined"}  # これ以外は画面で !! を付ける
PICOW_BAUD = 115200  # Pico W の送信機のシリアル（firmware/csi-tx-picow の SERIAL_BAUD）
DEFAULT_PICOW_PORT = "/dev/ttyACM0"  # --picow-port auto で探すポート
# --picow-port の既定。R が無線で Pico W を切り替えるので、USB からは送らない（両方から送ると、
# USB で先に動いた Pico W に R の命令が届かず pico=no_ack になる）。古い R のファームウェアでは auto
DEFAULT_PICOW_PORT_MODE = "none"
PICOW_WHO_TIMEOUT_SEC = 1.5  # Pico W の WHO の応答を待つ時間 [sec]
PICOW_WHO_PATTERN = re.compile(r"\[picow\] who role=tx-picow mac=([0-9a-f]{12}) channel=(\d+)")
PICOW_ACK_PATTERN = re.compile(r"\[picow\] ack cmd=CH channel=(\d+)")
PICOW_ERR_PATTERN = re.compile(r"\[picow\] err cmd=CH reason=(\w+)")
PICOW_CHANNEL_PATTERN = re.compile(r"\[picow\] (?:who|seq=).* channel=(\d+)")
MARK_COLORS = {"static": "tab:blue", "walk": "tab:red", "empty": "tab:gray", "mark": "k",
               "baseline": "tab:green"}
NON_INTERACTIVE_BACKENDS = {"agg", "pdf", "ps", "svg", "pgf", "cairo", "template"}


@dataclass(frozen=True)
class TxStatus:
    """送信機 1 台の受信状態（画面上部に 1 行ずつ出す）.

    Attributes:
        tx: 送信機の MAC。
        rate_hz: 直近 1 秒の受信数 [Hz]。
        rssi_dbm: 直近 1 秒の RSSI の平均 [dBm]（受けていなければ None）。
        missing: 直近 10 秒の欠番（連番から。数えられなければ None）。
        received: 直近 10 秒の受信数。
        silent_sec: 最後に受けてからの時間 [sec]。
        channel: 最後に受けたパケットのチャンネル（受信機がいたチャンネル）。
    """

    tx: str
    rate_hz: float
    rssi_dbm: float | None
    missing: int | None
    received: int
    silent_sec: float
    channel: int | None = None


@dataclass(frozen=True)
class Snapshot:
    """描画用にバッファから取り出した配列.

    Attributes:
        time_sec: 形状 (n,) の受信時刻 [sec]（最初のパケットから。32bit の一周を補正済み）。
        csi: 形状 (n, 56) の複素 CSI（HT-LTF ±1..±28、周波数順、生の int8 スケール）。
        rssi_dbm: 形状 (n,) の RSSI [dBm]。
        seq: 形状 (n,) の送信機の連番（不明は ``io.SEQ_UNKNOWN``）。
        channel: 形状 (n,) の受信したチャンネル番号。
    """

    time_sec: NDArray[np.float64]
    csi: NDArray[np.complex128]
    rssi_dbm: NDArray[np.int64]
    seq: NDArray[np.int64]
    channel: NDArray[np.int64]

    @property
    def amp(self) -> NDArray[np.float64]:
        """形状 (n, 56) の振幅（生の int8 スケール）."""
        amp: NDArray[np.float64] = np.abs(self.csi)
        return amp


class LiveBuffer:
    """受信スレッドが書き、描画側が読む、長さ固定のパケットバッファ."""

    def __init__(self, max_packets: int = BUFFER_PACKETS, tx: str | None = None,
                 ltf: io.Ltf = "htltf", peers: list[LiveBuffer] | None = None) -> None:
        """空のバッファを作る.

        Args:
            max_packets: 保持する最大パケット数（古いものから捨てる）。
            tx: 表示する送信機の MAC。None なら最初に来た送信機に絞る（送信機が 2 台の記録用）。
            ltf: 取り出す区間。11g の送信機（Pico W）は ``lltf``。LLTF では先頭の値が無効な
                +1 番（``first_word_invalid``）を除いた 51 本にする（NaN を計算に入れない）。
            peers: 並べて表示するほかのバッファ。``tx`` が None のとき、ほかのバッファが見ている
                送信機には付かない（2 台を左右に振り分ける）。
        """
        self._lock = threading.Lock()
        self._time_sec: deque[float] = deque(maxlen=max_packets)
        self._csi: deque[NDArray[np.complex128]] = deque(maxlen=max_packets)
        self._rssi: deque[int] = deque(maxlen=max_packets)
        self._seq: deque[int] = deque(maxlen=max_packets)
        self._channel: deque[int] = deque(maxlen=max_packets)
        self._prev_rx_us: int | None = None
        self._elapsed_us = 0
        self.last_arrival: float | None = None  # 最後に追加した時刻（time.monotonic）
        # 送信機ごとの受信の記録（表示していない送信機も含む）: (受けた時刻 monotonic, 連番, RSSI)
        # (受けた時刻, 連番, RSSI, チャンネル)
        self._per_tx: dict[str, deque[tuple[float, int, int, int]]] = {}
        # 表示している送信機のチャンネルが変わった時刻 [sec]（バッファの時間軸）とチャンネル。
        # 最初のパケットも入れる（表示を始めた時のチャンネル）
        self.channel_changes: list[tuple[float, int]] = []
        self._last_channel: int | None = None
        self.unsupported = 0  # HT20・非 STBC 以外で捨てたパケット数
        self.tx = io.colon_mac(tx) if tx is not None else None  # 表示している送信機
        self.other_tx = 0  # 表示していない送信機のパケット数
        self.peers: list[LiveBuffer] = peers if peers is not None else []
        self.ltf: io.Ltf = ltf
        index = io.valid_subcarrier_index(ltf)
        # LLTF の +1 番は first_word_invalid で NaN になる（2026-09-23: Pico W の行はすべて）
        self._keep = index != LLTF_INVALID_INDEX if ltf == "lltf" else np.ones(index.size, bool)
        self.subcarrier_index: NDArray[np.int64] = index[self._keep]

    def silent_sec(self, now: float | None = None) -> float | None:
        """表示している送信機から最後に受けてからの時間 [sec]（まだ受けていなければ None）.

        Args:
            now: 今の時刻（time.monotonic。テスト用）。
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            last = self.last_arrival
        return None if last is None else now - last

    def link_status(self, now: float | None = None) -> list[TxStatus]:
        """送信機ごとの受信状態（直近 1 秒のレート・RSSI、直近 10 秒の欠番）を返す.

        Args:
            now: 今の時刻（time.monotonic。テスト用）。

        Returns:
            送信機ごとの状態（MAC の順）。
        """
        now = time.monotonic() if now is None else now
        out = []
        with self._lock:
            items = [(tx, list(q)) for tx, q in sorted(self._per_tx.items())]
        for tx, rows in items:
            recent = [r for r in rows if now - r[0] <= RATE_WINDOW_SEC]
            window = [r for r in rows if now - r[0] <= STATUS_WINDOW_SEC]
            seq = np.array([r[1] for r in window], dtype=np.int64)
            try:
                missing = io.count_missing_by_seq(seq)
            except ValueError:
                missing = None
            silent = now - rows[-1][0] if rows else float("inf")
            out.append(TxStatus(tx=tx, rate_hz=len(recent) / RATE_WINDOW_SEC,
                                rssi_dbm=float(np.mean([r[2] for r in recent])) if recent else None,
                                missing=missing, received=len(window), silent_sec=silent,
                                channel=rows[-1][3] if rows else None))
        return out

    def append(self, rec: io.RxLine) -> None:
        """1 パケットを追加する（HT20・非 STBC 以外は数えて捨てる）.

        Args:
            rec: 解析済みの 1 行。
        """
        if rec.tx is not None:
            with self._lock:
                q = self._per_tx.setdefault(
                    rec.tx, deque(maxlen=int(STATUS_WINDOW_SEC * PACKET_RATE_HZ * 2)))
                q.append((time.monotonic(), rec.seq, rec.rssi_dbm, rec.channel))
            if self.tx is None and rec.tx not in {p.tx for p in self.peers}:
                self.tx = rec.tx  # 最初に来た送信機（ほかのバッファが見ていないもの）に絞る
            elif rec.tx != self.tx:
                self.other_tx += 1
                return
        try:
            csi = io.lines_to_capture([rec], ltf=self.ltf).csi[0, :, 0][self._keep]
        except ValueError:
            self.unsupported += 1
            return
        if self._prev_rx_us is not None:
            self._elapsed_us += (rec.rx_us - self._prev_rx_us) % io.RX_TIMESTAMP_MODULUS
        self._prev_rx_us = rec.rx_us
        with self._lock:
            self._time_sec.append(self._elapsed_us / 1e6)
            self._csi.append(csi)
            self._rssi.append(rec.rssi_dbm)
            self._seq.append(rec.seq)
            self._channel.append(rec.channel)
            self.last_arrival = time.monotonic()
            if rec.channel != self._last_channel:
                self.channel_changes.append((self._elapsed_us / 1e6, rec.channel))
                self._last_channel = rec.channel

    def snapshot(self) -> Snapshot | None:
        """今の中身を配列にして返す.

        Returns:
            バッファの写し。空なら None。
        """
        with self._lock:
            if not self._time_sec:
                return None
            return Snapshot(
                time_sec=np.array(self._time_sec, dtype=np.float64),
                csi=np.stack(self._csi),
                rssi_dbm=np.array(self._rssi, dtype=np.int64),
                seq=np.array(self._seq, dtype=np.int64),
                channel=np.array(self._channel, dtype=np.int64),
            )


class Recorder:
    """行の検査・集計と、（``--record`` のとき）ファイルへの書き出し."""

    def __init__(self, out: TextIO | None) -> None:
        """書き出し先を受け取る.

        Args:
            out: 記録ファイル。None なら集計だけ行う。
        """
        self._lock = threading.Lock()
        self._out = out
        self._null = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115  close() で閉じる
        self.stats = cap.CaptureStats()
        self.last: io.RxLine | None = None
        self.tx_names: dict[str, str] = dict(KNOWN_TX_NAMES)  # 送信機の MAC → 機種名

    @property
    def recording(self) -> bool:
        """ファイルに記録しているか."""
        return self._out is not None

    def handle(self, line: bytes) -> io.RxLine | None:
        """1 行を ``capture_csi.handle_line`` と同じ規則で扱う.

        Args:
            line: 改行を除いた 1 行。

        Returns:
            正しい CSI 行なら解析結果。それ以外（注記・壊れた行）は None。
        """
        if line.startswith(b"#"):
            parsed = io.parse_tx_header(line.decode("ascii", errors="replace"))
            if parsed is not None and parsed[1]:
                self.tx_names[parsed[0]] = parsed[1]
        with self._lock:
            before = self.stats.ok
            cap.handle_line(line, self._out or self._null, self.stats)
            if self.stats.ok == before:
                return None
        rec = io.parse_rx_line(line.decode("ascii"))  # 検査済みなので例外は出ない
        self.last = rec
        return rec

    def mark(self, label: str) -> str:
        """印の注記行を作り、記録中ならファイルに書く.

        Args:
            label: 印の種類（``MARK_KEYS`` の値）。

        Returns:
            書いた（記録していなければ書くはずだった）注記行。
        """
        seq = self.last.seq if self.last else io.SEQ_UNKNOWN
        rx_us = self.last.rx_us if self.last else -1
        text = (f"# mark label={label} seq={seq} rx_us={rx_us} "
                f"wall={datetime.now().isoformat(timespec='milliseconds')}")
        with self._lock:
            if self._out is not None:
                self._out.write(text + "\n")
                self._out.flush()
        return text

    def flush(self) -> None:
        """記録ファイルをディスクへ書き出す."""
        with self._lock:
            if self._out is not None:
                self._out.flush()

    def close(self) -> None:
        """集計を追記して閉じる."""
        with self._lock:
            if self._out is not None:
                self._out.write(f"# summary {self.stats.summary()}\n")
                self._out.close()
                self._out = None
            self._null.close()


@dataclass
class Follower:
    """R の切り替えに合わせて USB で ``CH`` を送る送信機（Pico W。ESP-NOW を受けられない）.

    Attributes:
        name: 画面に出す名前。
        write: その送信機へ書く関数（``serial.Serial.write``）。
        channel: その送信機が今いるチャンネル（ack・WHO・毎秒の報告で更新。不明は None）。
        start_channel: 最初に分かったチャンネル（終了時に戻す）。
        pending: 応答を待っている切り替え先。
        sent_at: 最後に ``CH`` を送った時刻（time.monotonic）。
        last_error: 最後の失敗（画面用・英語。成功で消える）。
    """

    name: str
    write: Callable[[bytes], object]
    channel: int | None = None
    start_channel: int | None = None
    pending: int | None = None
    sent_at: float = 0.0
    last_error: str = ""


class ChannelHopper:
    """受信機に ``CH`` を送ってチャンネルを切り替える（c キーと ``--hop``）.

    受信機は ``CH`` を送信機 T1 にも無線で伝え、T1 が新しいチャンネルで届くのを確かめてから
    ``# ack cmd=CH`` を返す（失敗なら ``# err cmd=CH``。元のチャンネルに戻る）。Pico W は無線の
    命令を受けられないので、``followers`` として USB で同時に ``CH`` を送り、R と食い違ったら
    R のチャンネルに合わせ直す。応答は受信スレッドが ``on_line`` / ``on_follower_line`` に渡す。
    命令を送るのと ``tick`` は描画側のスレッドから呼ぶ。
    """

    def __init__(self, write: Callable[[bytes], object] | None,
                 channels: Sequence[int] = DEFAULT_KEY_CHANNELS,
                 dwell_sec: float | None = None,
                 reply_timeout_sec: float = CH_REPLY_TIMEOUT_SEC,
                 followers: Sequence[Follower] = ()) -> None:
        """切り替えの順番と、自動で切り替える間隔を受け取る.

        Args:
            write: 受信機へ書く関数（``serial.Serial.write``）。None なら切り替えない（再生中）。
            channels: 回る順のチャンネル。
            dwell_sec: 自動で切り替える間隔 [sec]（None なら c キーのときだけ）。
            reply_timeout_sec: 応答を待つ時間 [sec]。
            followers: R に合わせて USB で切り替える送信機（Pico W）。

        Raises:
            ValueError: チャンネルが空・範囲外・重複、または間隔が短すぎる場合。
        """
        if not channels:
            raise ValueError("チャンネルが空です")
        bad = [c for c in channels if not MIN_WIFI_CHANNEL <= c <= MAX_WIFI_CHANNEL]
        if bad:
            raise ValueError(f"チャンネルは {MIN_WIFI_CHANNEL}〜{MAX_WIFI_CHANNEL}: {bad}")
        if len(set(channels)) != len(channels):
            raise ValueError(f"チャンネルが重複しています: {list(channels)}")
        if dwell_sec is not None and dwell_sec < MIN_DWELL_SEC:
            raise ValueError(f"切り替えの間隔は {MIN_DWELL_SEC} 秒以上: {dwell_sec}")
        self._lock = threading.Lock()
        self._write = write
        self.channels = list(channels)
        self.dwell_sec = dwell_sec
        self.reply_timeout_sec = reply_timeout_sec
        self.followers = list(followers)
        self.start_channel: int | None = None  # 最初に受けたチャンネル（終了時に戻す）
        self.current: int | None = None  # 受信機が今いるチャンネル（ack で更新）
        self.pending: int | None = None  # 応答を待っている切り替え先
        self._sent_at = 0.0  # 命令を送った時刻（time.monotonic）
        self._settled_at: float | None = None  # 最後に切り替えが済んだ（か失敗した）時刻
        self.last_error = ""  # 最後の失敗（画面用・英語。成功で消える）
        self.switches = 0  # 成功した切り替えの数
        self.pico_relay: str | None = None  # R が無線で切り替える Pico W の状態（pico= / 出来事）

    @property
    def enabled(self) -> bool:
        """切り替えられるか（シリアルにつながっている）."""
        return self._write is not None

    def observe(self, channel: int, now: float | None = None) -> None:
        """受けた CSI のチャンネルを知らせる（最初の 1 回で今のチャンネルと開始時刻が決まる）.

        Args:
            channel: CSI 行のチャンネル。
            now: 今の時刻（time.monotonic。テスト用）。
        """
        with self._lock:
            if self.current is None:
                self.current = channel
                self.start_channel = channel
                self._settled_at = time.monotonic() if now is None else now

    def next_channel(self) -> int:
        """今のチャンネルの次（今のチャンネルが並びに無ければ先頭）."""
        with self._lock:
            current = self.current
        if current in self.channels:
            return self.channels[(self.channels.index(current) + 1) % len(self.channels)]
        return self.channels[0]

    def _send_follower(self, follower: Follower, channel: int, now: float) -> None:
        """追従する送信機に ``CH`` を送る（書けなければ失敗として残す）."""
        follower.pending = channel
        follower.sent_at = now
        try:
            follower.write(f"CH {channel}\n".encode("ascii"))
        except (serial.SerialException, OSError) as e:
            follower.pending = None
            follower.last_error = f"CH {channel}: write failed ({e})"

    def request(self, channel: int | None = None, now: float | None = None) -> bool:
        """``CH`` を受信機と追従する送信機に送る（受信機の応答を待っている間は送らない）.

        Args:
            channel: 切り替え先（None なら ``next_channel``）。
            now: 今の時刻（time.monotonic。テスト用）。

        Returns:
            受信機に送ったか。
        """
        if self._write is None:
            self.last_error = "cannot switch channel while replaying"
            return False
        target = self.next_channel() if channel is None else channel
        if not MIN_WIFI_CHANNEL <= target <= MAX_WIFI_CHANNEL:
            raise ValueError(f"チャンネルは {MIN_WIFI_CHANNEL}〜{MAX_WIFI_CHANNEL}: {target}")
        now = time.monotonic() if now is None else now
        with self._lock:
            if self.pending is not None:
                return False
            self.pending = target
            self._sent_at = now
        try:
            self._write(f"CH {target}\n".encode("ascii"))
        except (serial.SerialException, OSError) as e:
            with self._lock:
                self.pending = None
                self._settled_at = now
                self.last_error = f"CH {target}: write failed ({e})"
            return False
        with self._lock:
            for follower in self.followers:  # 同時に切り替える（R の ack を待つと空白が延びる）
                self._send_follower(follower, target, now)
        return True

    def on_line(self, text: str, now: float | None = None) -> bool:
        """受信機の注記行を見て、``CH`` の応答なら状態を更新する.

        Args:
            text: 1 行。
            now: 今の時刻（time.monotonic。テスト用）。

        Returns:
            ``CH`` の応答だったか。
        """
        now = time.monotonic() if now is None else now
        if m := PICO_EVENT_PATTERN.search(text):
            with self._lock:
                self.pico_relay = m.group(1)
            return False
        if (CH_ACK_PATTERN.search(text) or CH_ERR_PATTERN.search(text)) \
                and (p := PICO_RELAY_PATTERN.search(text)):
            with self._lock:
                self.pico_relay = p.group(1)
        if m := CH_ACK_PATTERN.search(text):
            with self._lock:
                self.current = int(m.group(1))
                if self.start_channel is None:
                    self.start_channel = self.current
                self.pending = None
                self._settled_at = now
                self.last_error = ""
                self.switches += 1
            return True
        if m := CH_ERR_PATTERN.search(text):
            with self._lock:
                self.pending = None
                self._settled_at = now
                self.last_error = f"CH {m.group(2)} failed: {m.group(1)}"
            return True
        return False

    def on_follower_line(self, follower: Follower, text: str) -> bool:
        """追従する送信機の行を見て、チャンネルが分かれば状態を更新する.

        Args:
            follower: 行を出した送信機。
            text: 1 行。

        Returns:
            チャンネルに関わる行（``CH`` の応答・WHO・毎秒の報告）だったか。
        """
        with self._lock:
            if m := PICOW_ACK_PATTERN.search(text):
                follower.channel = int(m.group(1))
                follower.pending = None
                follower.last_error = ""
            elif m := PICOW_ERR_PATTERN.search(text):
                follower.pending = None
                follower.last_error = f"CH failed: {m.group(1)}"
            elif m := PICOW_CHANNEL_PATTERN.search(text):
                follower.channel = int(m.group(1))
            else:
                return False
            if follower.start_channel is None and follower.channel is not None:
                follower.start_channel = follower.channel
            return True

    def follower_failed(self, follower: Follower, message: str) -> None:
        """追従する送信機の失敗（読めなくなった等）を画面に出す.

        Args:
            follower: 失敗した送信機。
            message: 失敗の内容（画面用・英語）。
        """
        with self._lock:
            follower.last_error = message

    def tick(self, now: float | None = None) -> None:
        """時間切れを見て、追従する送信機を R に合わせ、``dwell_sec`` たてば次へ（描画ごと）.

        Args:
            now: 今の時刻（time.monotonic。テスト用）。
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            if self.pending is not None and now - self._sent_at > self.reply_timeout_sec:
                self.last_error = f"CH {self.pending}: no reply"
                self.pending = None
                self._settled_at = now
            for follower in self.followers:
                if follower.pending is not None and now - follower.sent_at > self.reply_timeout_sec:
                    follower.last_error = f"CH {follower.pending}: no reply"
                    follower.pending = None
                # R の切り替えが失敗して戻った時などに食い違いを直す（送り直しは応答待ちの後）
                if (self._write is not None and self.pending is None and follower.pending is None
                        and self.current is not None and follower.channel is not None
                        and follower.channel != self.current
                        and now - follower.sent_at > self.reply_timeout_sec):
                    self._send_follower(follower, self.current, now)
            due = (self.dwell_sec is not None and self.pending is None
                   and self._settled_at is not None and now - self._settled_at >= self.dwell_sec)
        if due and self._write is not None:
            self.request(now=now)

    def describe(self, now: float | None = None) -> str:
        """画面上部に出す 1 文（今のチャンネル・切り替え中・自動の予定・失敗）."""
        now = time.monotonic() if now is None else now
        with self._lock:
            parts = [f"RX channel: ch{self.current}" if self.current is not None
                     else "RX channel: ?"]
            if self.pending is not None:
                parts.append(f"-> ch{self.pending} switching ...")
            if self.pico_relay is not None:
                mark = "" if self.pico_relay in PICO_RELAY_OK else "!! "
                parts.append(f"PicoW(radio): {mark}{self.pico_relay}")
            for follower in self.followers:
                ch = "?" if follower.channel is None else f"ch{follower.channel}"
                parts.append(f"{follower.name}: {ch}"
                             + (f" (!! {follower.last_error})" if follower.last_error else ""))
            if self.dwell_sec is not None:
                hop = ",".join(str(c) for c in self.channels)
                wait = ""
                if self._settled_at is not None and self.pending is None:
                    left_sec = max(0.0, self.dwell_sec - (now - self._settled_at))
                    wait = f", next in {left_sec:.0f} s"
                parts.append(f"hop {hop} every {self.dwell_sec:g} s{wait}")
            if self.last_error:
                parts.append(f"!! {self.last_error}")
        return "  ".join(parts)


def wait_reply(ser: serial.Serial, handle: Callable[[str], bool], timeout_sec: float) -> str | None:
    """``handle`` が True を返す行が来るまで読む（受信スレッドを止めてから使う）.

    Args:
        ser: 読むシリアル。
        handle: 1 行を受け取り、待っていた行なら True を返す関数。
        timeout_sec: 待つ時間 [sec]。

    Returns:
        待っていた行（時間切れなら None）。
    """
    assembler = cap.LineAssembler()
    end = time.monotonic() + timeout_sec
    while time.monotonic() < end:
        for line in assembler.feed(ser.read(cap.READ_CHUNK_BYTES)):
            text = line.decode("ascii", errors="replace")
            if handle(text):
                return text
    return None


def restore_channel(ser: serial.Serial, hopper: ChannelHopper) -> None:
    """起動時と違うチャンネルにいれば戻す（受信スレッドを止めてから呼ぶ）.

    戻さないと、次に使うとき T1 と R1 が ch11 以外のままで、Pico W（ch11 で起動）と食い違う。

    Args:
        ser: 受信機のシリアル。
        hopper: 切り替えの状態。
    """
    start = hopper.start_channel
    if start is None or (hopper.current == start and hopper.pending is None):
        return
    try:
        ser.write(f"CH {start}\n".encode("ascii"))
        text = wait_reply(ser, hopper.on_line, hopper.reply_timeout_sec)
    except (serial.SerialException, OSError) as e:
        print(f"ch{start} に戻せませんでした: {e}", file=sys.stderr)
        return
    if text is None:
        print(f"ch{start} に戻す命令に応答がありません。WHO か CH {start} で確かめてください",
              file=sys.stderr)
    elif hopper.current == start and not hopper.last_error:
        print(f"チャンネルを ch{start} に戻しました", file=sys.stderr)
    else:
        print(f"ch{start} に戻せませんでした: {text.strip()}", file=sys.stderr)


def restore_follower(ser: serial.Serial, hopper: ChannelHopper, follower: Follower) -> None:
    """追従する送信機が起動時と違うチャンネルにいれば戻す（読むスレッドを止めてから呼ぶ）.

    Args:
        ser: その送信機のシリアル。
        hopper: 切り替えの状態（応答の解釈に使う）。
        follower: 戻す送信機。
    """
    start = follower.start_channel
    if start is None or (follower.channel == start and follower.pending is None):
        return

    def is_reply(text: str) -> bool:
        return bool(PICOW_ACK_PATTERN.search(text) or PICOW_ERR_PATTERN.search(text)) \
            and hopper.on_follower_line(follower, text)

    try:
        ser.write(f"CH {start}\n".encode("ascii"))
        text = wait_reply(ser, is_reply, hopper.reply_timeout_sec)
    except (serial.SerialException, OSError) as e:
        print(f"{follower.name} を ch{start} に戻せませんでした: {e}", file=sys.stderr)
        return
    if text is not None and follower.channel == start:
        print(f"{follower.name} を ch{start} に戻しました", file=sys.stderr)
    else:
        print(f"{follower.name} を ch{start} に戻せませんでした: {(text or 'no reply').strip()}",
              file=sys.stderr)


def open_picow(port: str, timeout_sec: float = PICOW_WHO_TIMEOUT_SEC) -> serial.Serial | None:
    """Pico W の送信機のポートを開き、WHO の応答で確かめる（違えば閉じて None）.

    Args:
        port: シリアルポート。
        timeout_sec: WHO の応答を待つ時間 [sec]。

    Returns:
        開いたシリアル（Pico W でなければ None）。
    """
    try:
        ser = serial.Serial(port, PICOW_BAUD, timeout=cap.READ_TIMEOUT_SEC)
    except serial.SerialException as e:
        print(f"Pico W のポートを開けません: {e}", file=sys.stderr)
        return None
    try:
        ser.reset_input_buffer()
        ser.write(b"WHO\n")
        if wait_reply(ser, lambda t: bool(PICOW_WHO_PATTERN.search(t)), timeout_sec) is not None:
            return ser
    except (serial.SerialException, OSError) as e:
        print(f"Pico W に WHO を送れません: {e}", file=sys.stderr)
    ser.close()
    print(f"{port} は Pico W の送信機として応答しません（チャンネルは合わせません）",
          file=sys.stderr)
    return None


def follower_loop(ser: serial.Serial, hopper: ChannelHopper, follower: Follower,
                  stop: threading.Event) -> None:
    """追従する送信機の行を読み、チャンネルの報告を ``hopper`` に渡すスレッドの本体.

    Args:
        ser: その送信機のシリアル。
        hopper: 切り替えの状態。
        follower: 読む送信機。
        stop: 止める合図。
    """
    assembler = cap.LineAssembler()
    try:
        while not stop.is_set():
            for line in assembler.feed(ser.read(cap.READ_CHUNK_BYTES)):
                hopper.on_follower_line(follower, line.decode("ascii", errors="replace"))
    except (serial.SerialException, OSError) as e:
        hopper.follower_failed(follower, f"read error: {e}")


def serial_chunks(ser: serial.Serial, reset: bool, stop: threading.Event) -> Iterator[bytes]:
    """シリアルから読んだ断片を順に返す.

    Args:
        ser: 開いたシリアルポート。
        reset: 最初に受信機をリセットするか（起動時の設定行が記録に残る）。
        stop: 止める合図。

    Yields:
        読んだバイト列（空のこともある）。
    """
    if reset:
        # リセット前にたまった行を捨てる（2026-09-23 の対策。capture_csi と共通の関数）
        cap.reset_receiver(ser)
    while not stop.is_set():
        yield ser.read(cap.READ_CHUNK_BYTES)


def replay_chunks(path: Path, stop: threading.Event, speed: float = 1.0) -> Iterator[bytes]:
    """記録ファイルの行を、受信時刻の間隔どおりに返す.

    Args:
        path: ``capture_csi.py`` か ``--record`` の記録。
        stop: 止める合図。
        speed: 再生速度の倍率（0 以下なら待たずに流す）。

    Yields:
        1 行分のバイト列（改行つき）。
    """
    started = time.monotonic()
    first_us: int | None = None
    elapsed_us = 0
    prev_us: int | None = None
    with io.open_rx_text(path) as f:  # .csv.zst も展開しながら読む
        for text in f:
            if stop.is_set():
                return
            line = text.encode("ascii")
            if speed > 0 and text.startswith(io.RX_LINE_PREFIX):
                try:
                    rx_us = io.parse_rx_line(text).rx_us
                except ValueError:
                    rx_us = None
                if rx_us is not None:
                    if first_us is None:
                        first_us = rx_us
                    elif prev_us is not None:
                        elapsed_us += (rx_us - prev_us) % io.RX_TIMESTAMP_MODULUS
                    prev_us = rx_us
                    wait = elapsed_us / 1e6 / speed - (time.monotonic() - started)
                    if wait > 0:
                        time.sleep(wait)
            yield line


def read_loop(chunks: Iterator[bytes], recorder: Recorder,
              buffer: LiveBuffer | list[LiveBuffer], status: dict[str, str],
              end_message: str | None = None, hopper: ChannelHopper | None = None) -> None:
    """受信スレッドの本体。断片を行にし、検査して、バッファに積む.

    Args:
        chunks: ``serial_chunks`` か ``replay_chunks``。
        recorder: 行の検査・記録。
        buffer: 描画用のバッファ（並べて表示するときは一覧。全部に同じ行を配る）。
        status: 描画側へ伝える状態（``error`` に失敗の内容、``done`` に再生の終わり）。
            画面に出すので英語で書く（既定のフォントに日本語が無い）。
        end_message: 入力が尽きたときに ``done`` に入れる文（再生用。シリアルでは None）。
        hopper: チャンネルの切り替え（``CH`` の応答と、受けたチャンネルを知らせる）。
    """
    assembler = cap.LineAssembler()
    buffers = buffer if isinstance(buffer, list) else [buffer]
    buffer = buffers[0]  # 見出しの主の送信機は先頭（左）のバッファに付ける
    try:
        for chunk in chunks:
            for line in assembler.feed(chunk):
                if buffer.tx is None and line.startswith(b"#"):
                    # 見出しに主の送信機があれば、それをグラフに表示する（送信機 2 台のとき）
                    m = io.PRIMARY_TX_PATTERN.match(line.decode("ascii", errors="replace"))
                    if m:
                        buffer.tx = io.colon_mac(m.group(1) or m.group(2))
                if hopper is not None and line.startswith(b"#"):
                    hopper.on_line(line.decode("ascii", errors="replace"))
                rec = recorder.handle(line)
                if rec is not None:
                    if hopper is not None:
                        hopper.observe(rec.channel)
                    for b in buffers:
                        b.append(rec)
            recorder.flush()
        if end_message is not None:
            status["done"] = end_message
    except (serial.SerialException, OSError) as e:
        status["error"] = f"read error: {e}"


def normalized(snap: Snapshot
               ) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.int64]]:
    """パケットごとの倍率を打ち消した振幅を返す（サブキャリア平均が 0 のパケットは除く）.

    Args:
        snap: バッファの写し。

    Returns:
        形状 (n,) の時刻 [sec]、形状 (n, 56) の正規化振幅（単位なし）、形状 (n,) のチャンネル。
    """
    ok = snap.amp.mean(axis=1) > 0
    norm = features.normalize_per_packet(snap.amp[ok][:, :, np.newaxis])[:, :, 0]
    return snap.time_sec[ok], norm, snap.channel[ok]


def reference_by_channel(rows: NDArray[np.float64], channels: NDArray[np.int64],
                         base_rows: NDArray[np.float64] | None = None,
                         base_channels: NDArray[np.int64] | None = None) -> NDArray[np.float64]:
    """各行の「ずれ」の基準の形を、その行と同じチャンネルの平均から求める.

    チャンネルが違うと周波数応答の形そのものが違う（実験05b）ので、全部を 1 つの平均にすると
    切り替えた区間が丸ごと「ずれ」に見える。チャンネルごとに、静止基準にそのチャンネルの行が
    あればその平均、なければ ``rows`` のうち同じチャンネルの行の平均を基準にする。

    Args:
        rows: 形状 (n, 本数) の正規化振幅。
        channels: 形状 (n,) の各行のチャンネル。
        base_rows: 形状 (m, 本数) の静止基準（なければ None）。
        base_channels: 形状 (m,) の静止基準の各行のチャンネル。

    Returns:
        形状 (n, 本数) の、各行に対応する基準の形。

    Raises:
        ValueError: 行とチャンネルの数が合わない場合。
    """
    if rows.shape[0] != channels.shape[0]:
        raise ValueError(f"rows {rows.shape} と channels {channels.shape} の数が合いません")
    if (base_rows is None) != (base_channels is None):
        raise ValueError("base_rows と base_channels は両方渡すか、両方 None にする")
    if base_rows is not None and base_channels is not None \
            and base_rows.shape[0] != base_channels.shape[0]:
        raise ValueError(f"base_rows {base_rows.shape} と base_channels "
                         f"{base_channels.shape} の数が合いません")
    ref = np.empty_like(rows)
    for ch in np.unique(channels):
        here = channels == ch
        source = rows[here]
        if base_rows is not None and base_channels is not None:
            base_here = base_channels == ch
            if base_here.any():
                source = base_rows[base_here]
        ref[here] = source.mean(axis=0)
    return ref


class MotionTracker:
    """動きの倍率（PC1 の 1 秒標準偏差 ÷ 静止基準）を求める.

    主軸は履歴から毎回求め直し（前回と向きを揃える）、基準区間を今の主軸に投影し直して割る。
    チャンネルが違うと周波数応答の形が違うので、主軸・静止基準・直近の窓はすべて今いる
    チャンネルの行だけで求める（切り替えそのものを「動き」に数えない）。
    1 台の表示（``LiveView``）と 2 台を並べる表示（``CompareView``）で共通に使う。
    """

    def __init__(self) -> None:
        """基準のない状態で始める."""
        self.axes: dict[int, NDArray[np.float64]] = {}  # チャンネルごとの PC1 の主軸（本数,）
        self.baseline: NDArray[np.float64] | None = None  # 静止基準の正規化振幅 (n, 本数)
        self.baseline_channel: NDArray[np.int64] | None = None  # 静止基準の各行のチャンネル (n,)
        self.last_ratio: float | None = None  # 最新の倍率（基準がなければ None）
        self.note = ""  # 倍率を出せない理由（画面用・英語。出せたときは空）

    def set_baseline(self, t: NDArray[np.float64], norm: NDArray[np.float64],
                     channel: NDArray[np.int64], now: float) -> bool:
        """直近 ``BASELINE_SEC`` 秒を静止基準にする.

        Args:
            t: 形状 (n,) の時刻 [sec]。
            norm: 形状 (n, 本数) の正規化振幅。
            channel: 形状 (n,) の各行のチャンネル。
            now: 最新の時刻 [sec]。

        Returns:
            登録できたか（パケットが ``MIN_BASELINE_PACKETS`` 未満なら False）。
        """
        recent = t >= now - BASELINE_SEC
        if recent.sum() < MIN_BASELINE_PACKETS:
            print(f"基準にはパケットが {MIN_BASELINE_PACKETS} 以上必要です: {recent.sum()}",
                  file=sys.stderr)
            return False
        self.baseline = norm[recent]
        self.baseline_channel = channel[recent]
        return True

    def baseline_std(self, axis: NDArray[np.float64], channel: int) -> float | None:
        """静止基準のうち ``channel`` の行を主軸に投影し、1 秒窓標準偏差の中央値を返す.

        Returns:
            標準偏差の中央値（未登録か、そのチャンネルの行が 1 秒分に満たなければ None）。
        """
        if self.baseline is None or self.baseline_channel is None:
            return None
        rows = self.baseline[self.baseline_channel == channel]
        if len(rows) < MOTION_WINDOW_PACKETS:
            return None
        proj = rows @ axis
        stds = features.windowed_temporal_std(proj.reshape(-1, 1, 1), MOTION_WINDOW_PACKETS)
        return max(float(np.median(stds)), MIN_BASELINE_STD)

    def fit_axis(self, norm: NDArray[np.float64], channel: int) -> NDArray[np.float64] | None:
        """``channel`` の行で PC1 の主軸を求め、そのチャンネルの前回と向きを揃える.

        Args:
            norm: 形状 (n, 本数) の、``channel`` の行だけの正規化振幅。
            channel: チャンネル。

        Returns:
            主軸（求まらなければそのチャンネルの前回の主軸。なければ None）。
        """
        prev = self.axes.get(channel)
        try:
            pca = features.pca_subcarriers(norm[:, :, np.newaxis], 1)
        except ValueError:
            return prev
        axis: NDArray[np.float64] = pca.components[0]
        if prev is not None and float(axis @ prev) < 0:
            axis = -axis  # 符号の反転で値が跳ばないようにする
        self.axes[channel] = axis
        return axis

    def update(self, norm: NDArray[np.float64], channel: NDArray[np.int64],
               silent_sec: float | None = None) -> None:
        """今のチャンネルの直近 1 秒の PC1 標準偏差を静止基準で割り、``last_ratio`` を更新する.

        直近 1 秒はチャンネルが切り替わってから連続した行に限る（切り替え直後は待つ）。
        受信が ``MOTION_STALE_SEC`` より長く止まっていれば、古い窓の倍率は出さない。

        Args:
            norm: 形状 (n, 本数) の正規化振幅（履歴全体）。
            channel: 形状 (n,) の各行のチャンネル。
            silent_sec: 最後に受けてからの時間 [sec]（None なら見ない）。

        Raises:
            ValueError: 行とチャンネルの数が合わない場合。
        """
        if norm.shape[0] != channel.shape[0]:
            raise ValueError(f"norm {norm.shape} と channel {channel.shape} の数が合いません")
        self.last_ratio = None
        self.note = ""
        if silent_sec is not None and silent_sec > MOTION_STALE_SEC:
            self.note = f"no reception {silent_sec:.1f} s"
            return
        if len(norm) < MOTION_WINDOW_PACKETS:
            return
        ch = int(channel[-1])
        other = np.flatnonzero(channel != ch)
        run = len(channel) - (int(other[-1]) + 1 if other.size else 0)  # 切り替えてからの行数
        if run < MOTION_WINDOW_PACKETS:
            self.note = f"ch{ch} settling"
            return
        axis = self.fit_axis(norm[channel == ch], ch)
        base = None if axis is None else self.baseline_std(axis, ch)
        if axis is None or base is None:
            if self.baseline is not None:
                self.note = f"no baseline on ch{ch} (press b)"
            return
        score = norm[-MOTION_WINDOW_PACKETS:] @ axis
        self.last_ratio = float(np.std(score)) / base

    def reference(self, recent: NDArray[np.float64],
                  channel: NDArray[np.int64]) -> NDArray[np.float64]:
        """各行の「ずれ」の基準の形（同じチャンネルの静止基準の平均。なければ直近の平均）.

        Args:
            recent: 形状 (n, 本数) の正規化振幅。
            channel: 形状 (n,) の各行のチャンネル。

        Returns:
            形状 (n, 本数) の基準の形（``reference_by_channel``）。
        """
        return reference_by_channel(recent, channel, self.baseline, self.baseline_channel)

    def describe(self, threshold: float) -> str:
        """倍率を短い文にする（基準がなければ案内）."""
        if self.last_ratio is None:
            return f"MOTION -- {self.note}".rstrip()
        state = "MOVING" if self.last_ratio > threshold else "still"
        return f"MOTION x{self.last_ratio:.1f} {state}"


def tx_label(tx: str | None, names: dict[str, str]) -> str:
    """送信機の表示名（機種名と MAC。分からなければ MAC だけ）."""
    if tx is None:
        return "(waiting)"
    mac = tx.replace(":", "")
    return f"{names[tx]} ({mac})" if tx in names else mac


def link_status_lines(buffer: LiveBuffer, shown: set[str | None],
                      names: dict[str, str] | None = None) -> list[str]:
    """送信機ごとの受信状態を 1 行ずつ作る（表示中の送信機に ``*`` を付ける）.

    Args:
        buffer: 送信機ごとの受信を記録しているバッファ（どのバッファも全送信機を記録する）。
        shown: グラフに表示している送信機。
        names: 送信機の MAC → 機種名。

    Returns:
        行の一覧。
    """
    out = []
    for st in buffer.link_status():
        mark = "*" if st.tx in shown else " "
        rssi = f"{st.rssi_dbm:6.1f} dBm" if st.rssi_dbm is not None else "    -    "
        miss = "?" if st.missing is None else f"{st.missing}/{st.received}"
        state = "OK" if st.silent_sec < NO_DATA_WARN_SEC else f"!! no CSI {st.silent_sec:.0f} s"
        ch = f"ch{st.channel:<2d}" if st.channel is not None else "ch? "
        out.append(f"{mark}{tx_label(st.tx, names or {}):28s}: {ch}  {st.rate_hz:5.0f} Hz  "
                   f"{rssi}  missing {miss} (10 s)  {state}")
    return out


def receive_note(buffer: LiveBuffer, status: dict[str, str]) -> str:
    """受信スレッドの状態（エラー・終わり・受信待ち・途切れ）を 1 文にする."""
    silent = None if buffer.last_arrival is None else time.monotonic() - buffer.last_arrival
    if "error" in status:
        return f"!! {status['error']}"
    if "done" in status:
        return status["done"]
    if silent is None:
        return "waiting for CSI ... (check TX power / channel)"
    if silent > NO_DATA_WARN_SEC:
        return f"!! no CSI for {silent:.0f} s (check TX power / channel)"
    return ""


def channel_key_hint(hopper: ChannelHopper | None) -> str:
    """キーの案内に足す c キーの説明（切り替えられないときは空）."""
    if hopper is None or not hopper.enabled:
        return ""
    return f" {CHANNEL_KEY}=next channel ({','.join(str(c) for c in hopper.channels)})"


def resample_to_grid(t: NDArray[np.float64], rows: NDArray[np.float64], t_end: float,
                     span_sec: float = WATERFALL_SEC, n_cols: int = WATERFALL_COLUMNS,
                     tolerance_sec: float = GAP_TOLERANCE_SEC) -> NDArray[np.float64]:
    """時刻がばらばらの行を、等間隔の時間の格子に並べ直す（途切れは NaN）.

    ウォーターフォールをパケットの並びのまま描くと、受信が途切れた区間（チャンネルの切り替えで
    別のチャンネルにいた間など）が詰まって見えなくなる。各列にはその列の中央の時刻に最も近い
    パケットを入れ、``tolerance_sec`` より離れていれば NaN（空白）にする。

    Args:
        t: 形状 (n,) の時刻 [sec]（昇順）。
        rows: 形状 (n, 本数) の値。
        t_end: 格子の右端の時刻 [sec]（最新）。
        span_sec: 格子の長さ [sec]。
        n_cols: 列の数。
        tolerance_sec: 途切れとみなす時刻の差 [sec]。

    Returns:
        形状 (n_cols, 本数) の値（途切れた列と、データより前の列は NaN）。

    Raises:
        ValueError: 形が合わない、または列の数が 1 未満の場合。
    """
    if t.shape[0] != rows.shape[0] or rows.ndim != 2:
        raise ValueError(f"t {t.shape} と rows {rows.shape} の形が合いません")
    if n_cols < 1:
        raise ValueError(f"n_cols は 1 以上: {n_cols}")
    grid = np.full((n_cols, rows.shape[1]), np.nan)
    if t.size == 0:
        return grid
    width = span_sec / n_cols
    centers = t_end - span_sec + width * (np.arange(n_cols) + 0.5)
    right = np.clip(np.searchsorted(t, centers), 0, t.size - 1)
    left = np.clip(right - 1, 0, t.size - 1)
    nearest = np.where(np.abs(t[left] - centers) <= np.abs(t[right] - centers), left, right)
    ok = np.abs(t[nearest] - centers) <= tolerance_sec
    grid[ok] = rows[nearest[ok]]
    return grid


def draw_channel_changes(buffer: LiveBuffer, axes: list[Any], drawn: int) -> int:
    """バッファの ``channel_changes`` のうち、まだ描いていない分を縦線と文字で描く.

    Args:
        buffer: 表示している送信機のバッファ。
        axes: 線を引くグラフ（時間軸が同じもの）。
        drawn: すでに描いた数。

    Returns:
        描いた後の数（次に渡す）。
    """
    changes = buffer.channel_changes[drawn:]
    for t, channel in changes:
        for ax in axes:
            ax.axvline(t, color=CHANNEL_LINE_COLOR, lw=1.2, ls="--", alpha=0.9)
            ax.text(t, 0.02, f" ch{channel}", transform=ax.get_xaxis_transform(),
                    color=CHANNEL_LINE_COLOR, fontsize=9, va="bottom", clip_on=True,
                    fontweight="bold")
    return drawn + len(changes)


class LiveView:
    """2×2 の画面（振幅・基準からのずれ・IQ 軌跡・円形レーダー）と、キー操作."""

    def __init__(self, buffer: LiveBuffer, recorder: Recorder, status: dict[str, str],
                 title: str, ratio_threshold: float = DEFAULT_RATIO_THRESHOLD,
                 hopper: ChannelHopper | None = None) -> None:
        """画面を組み立てる.

        Args:
            buffer: 描画用のバッファ。
            recorder: 集計と印の書き出し。
            status: 受信スレッドの状態。
            title: 画面上部に出す説明。
            ratio_threshold: 「動きあり」とみなす倍率（静止基準に対する PC1 の 1 秒標準偏差の比）。
            hopper: チャンネルの切り替え（None なら c キーは効かない）。
        """
        self.buffer = buffer
        self.recorder = recorder
        self.status = status
        self.title = title
        self.ratio_threshold = ratio_threshold
        self.hopper = hopper
        self.marks: list[tuple[float, str]] = []  # (time_sec, label)
        self.sc = buffer.subcarrier_index
        self.motion = MotionTracker()
        self._channel_drawn = 0  # 描いたチャンネルの切り替えの数

        self.fig = plt.figure(figsize=(14, 9), layout="constrained")
        self.text = self.fig.suptitle(title, family=plotting.family_with_japanese("monospace"),
                                      fontsize=10)
        grid = self.fig.add_gridspec(2, 2)
        self.ax_wf = self.fig.add_subplot(grid[0, 0])
        self.ax_dev = self.fig.add_subplot(grid[0, 1])
        self.ax_iq = self.fig.add_subplot(grid[1, 0])
        self.ax_rd = self.fig.add_subplot(grid[1, 1], projection="polar")

        extent = (0.0, 1.0, float(self.sc[0]), float(self.sc[-1]))
        blank = np.zeros((len(self.sc), 2))
        self.image = self.ax_wf.imshow(blank, aspect="auto", origin="lower", cmap="viridis",
                                       extent=extent)
        self.fig.colorbar(self.image, ax=self.ax_wf)
        self.ax_wf.set(title=f"normalized amplitude (last {WATERFALL_SEC:.0f} s)",
                       xlabel="time [s]", ylabel="subcarrier index")
        self.image_dev = self.ax_dev.imshow(blank, aspect="auto", origin="lower",
                                            cmap="RdBu_r", vmin=-DEVIATION_CLIM,
                                            vmax=DEVIATION_CLIM, extent=extent)
        self.fig.colorbar(self.image_dev, ax=self.ax_dev, label="deviation (normalized)")
        self.ax_dev.set(xlabel="time [s]", ylabel="subcarrier index")

        colors = plt.get_cmap("turbo")(np.linspace(0.0, 1.0, len(self.sc)))
        self.iq_trails = [self.ax_iq.plot([], [], "-", color=c, lw=0.6, alpha=0.6)[0]
                          for c in colors]
        self.iq_heads = self.ax_iq.scatter(np.zeros(len(self.sc)), np.zeros(len(self.sc)),
                                           c=colors, s=14, zorder=3)
        self.ax_iq.set(title=f"IQ trails of {len(self.sc)} subcarriers "
                             f"(last {TRAIL_SEC:.0f} s, linear phase removed, color = index)",
                       xlabel="I (normalized)", ylabel="Q (normalized)", aspect="equal")

        theta = np.linspace(0.0, 2 * np.pi, len(self.sc), endpoint=False)
        self.theta = np.append(theta, theta[0])  # 輪を閉じる
        self.radar_traces = [self.ax_rd.plot([], [], lw=0.7, alpha=0.3)[0]
                             for _ in range(RADAR_TRACES)]
        (self.radar_ref,) = self.ax_rd.plot([], [], "k--", lw=1.0)
        self.ax_rd.set_ylim(*RADAR_RLIM)
        self.ax_rd.set_xticks(theta[::RADAR_TICK_STEP])
        self.ax_rd.set_xticklabels([str(k) for k in self.sc[::RADAR_TICK_STEP]], fontsize=8)
        self.ax_rd.set_title(f"radar ring: normalized amplitude, last {TRAIL_SEC:.0f} s "
                             "(dashed = reference)", fontsize=10)
        self.fig.canvas.mpl_connect("key_press_event", self.on_key)

    def on_key(self, event: object) -> None:
        """数字キーで印を付け、``b`` で静止基準を登録する.

        Args:
            event: matplotlib の KeyEvent（``key`` 属性を使う）。
        """
        key = str(getattr(event, "key", ""))
        if key == BASELINE_KEY:
            self.set_baseline()
            return
        if key == CHANNEL_KEY and self.hopper is not None:
            self.hopper.request()
            return
        label = MARK_KEYS.get(key)
        snap = self.buffer.snapshot()
        if label is None or snap is None:
            return
        self.add_mark(float(snap.time_sec[-1]), label)

    def add_mark(self, time_sec: float, label: str) -> None:
        """印を記録し、時間軸のあるグラフに縦線を引く.

        Args:
            time_sec: 印の時刻 [sec]（バッファの時間軸）。
            label: 印の種類。
        """
        print(self.recorder.mark(label), file=sys.stderr)
        self.marks.append((time_sec, label))
        for axis in (self.ax_wf, self.ax_dev):
            axis.axvline(time_sec, color=MARK_COLORS[label], lw=1.5, alpha=0.9)
            axis.text(time_sec, 1.0, f" {label}", transform=axis.get_xaxis_transform(),
                      color=MARK_COLORS[label], fontsize=8, va="top", clip_on=True)

    @property
    def last_ratio(self) -> float | None:
        """最新の動きの倍率（基準がなければ None）."""
        return self.motion.last_ratio

    @property
    def baseline(self) -> NDArray[np.float64] | None:
        """静止基準の正規化振幅（未登録なら None）."""
        return self.motion.baseline

    def set_baseline(self) -> bool:
        """直近 ``BASELINE_SEC`` 秒を静止基準として登録する（誰も動いていない間に押す）.

        Returns:
            登録できたか（データが足りなければ False）。
        """
        snap = self.buffer.snapshot()
        if snap is None:
            return False
        t, norm, channel = normalized(snap)
        now = float(snap.time_sec[-1])
        if not self.motion.set_baseline(t, norm, channel, now):
            return False
        self.add_mark(now, "baseline")
        return True

    def update(self) -> None:
        """バッファの最新の中身で画面を描き直す."""
        if self.hopper is not None:
            self.hopper.tick()
        snap = self.buffer.snapshot()
        if snap is None:
            self.text.set_text(self.status_text(None))
            return
        now = float(snap.time_sec[-1])
        t_ok, norm, ch_ok = normalized(snap)
        self.motion.update(norm, ch_ok, self.buffer.silent_sec())

        recent = t_ok >= now - WATERFALL_SEC
        if recent.sum() >= 2:
            wf = norm[recent]
            # 等間隔の時間の格子に並べ直し、受信が途切れた区間は空白（NaN）で描く
            grid = resample_to_grid(t_ok[recent], wf, now)
            extent = (now - WATERFALL_SEC, now, float(self.sc[0]), float(self.sc[-1]))
            self.image.set_data(grid.T)
            self.image.set_extent(extent)
            self.image.set_clim(*np.percentile(wf, CLIM_PERCENTILES))
            self.ax_wf.set_xlim(now - WATERFALL_SEC, now)
            # 基準は行ごとに同じチャンネルの平均（チャンネルの違いを「ずれ」に数えない）
            ref = self.motion.reference(wf, ch_ok[recent])
            dev = resample_to_grid(t_ok[recent], wf - ref, now)
            self.image_dev.set_data(dev.T)
            self.image_dev.set_extent(extent)
            self.ax_dev.set_xlim(now - WATERFALL_SEC, now)
            self.ax_dev.set_title("deviation from " + ("static baseline" if self.baseline
                                                         is not None else
                                                         f"mean of last {WATERFALL_SEC:.0f} s "
                                                         "(press b to set baseline)"))
            self.draw_radar(norm[t_ok >= now - TRAIL_SEC], ref[-1])  # 今のチャンネルの基準

        self._channel_drawn = draw_channel_changes(self.buffer, [self.ax_wf, self.ax_dev],
                                                   self._channel_drawn)
        self.draw_iq(snap.csi[snap.time_sec >= now - TRAIL_SEC])
        self.text.set_text(self.status_text(snap))
        moving = self.last_ratio is not None and self.last_ratio > self.ratio_threshold
        self.text.set_color("tab:red" if moving else "black")

    def draw_iq(self, csi: NDArray[np.complex128]) -> None:
        """左下: 直近の各サブキャリアの複素値の軌跡を描く.

        位相はパケットごとに CFO 等でランダムに回るため、サブキャリア方向の直線成分を除く
        （``preprocess.sanitize_phase``）。振幅はパケットごとに平均 1 に正規化する。

        Args:
            csi: 形状 (n, 56) の複素 CSI。
        """
        amp = np.abs(csi)
        ok = amp.mean(axis=1) > 0
        if ok.sum() < 2:
            return
        phase_rad = preprocess.sanitize_phase(csi[ok][:, :, np.newaxis])[:, :, 0]
        amp_norm = amp[ok] / amp[ok].mean(axis=1, keepdims=True)
        z = amp_norm * np.exp(1j * phase_rad)
        for j, line in enumerate(self.iq_trails):
            line.set_data(z[:, j].real, z[:, j].imag)
        self.iq_heads.set_offsets(np.column_stack([z[-1].real, z[-1].imag]))
        center = complex(z.mean())
        half = max(float(np.abs(z - center).max()) * Y_HEADROOM, IQ_MIN_HALF_WIDTH)
        self.ax_iq.set_xlim(center.real - half, center.real + half)
        self.ax_iq.set_ylim(center.imag - half, center.imag + half)

    def draw_radar(self, recent: NDArray[np.float64], ref: NDArray[np.float64]) -> None:
        """右下: 直近の正規化振幅を円周に並べ、間引いて重ね描きする.

        Args:
            recent: 形状 (n, 56) の直近 ``TRAIL_SEC`` 秒の正規化振幅。
            ref: 形状 (56,) の基準の形（点線）。
        """
        moving = self.last_ratio is not None and self.last_ratio > self.ratio_threshold
        color = ("tab:gray" if self.last_ratio is None
                 else "tab:red" if moving else "tab:blue")
        rows = recent[::-1][::max(len(recent) // RADAR_TRACES, 1)][:RADAR_TRACES]
        for k, line in enumerate(self.radar_traces):
            if k < len(rows):
                line.set_data(self.theta, np.append(rows[k], rows[k][0]))
                line.set_color(color)
            else:
                line.set_data([], [])
        self.radar_ref.set_data(self.theta, np.append(ref, ref[0]))

    def status_text(self, snap: Snapshot | None) -> str:
        """画面上部に出す、動きの判定と受信状態の文を作る.

        Args:
            snap: バッファの写し（まだ受信していなければ None）。

        Returns:
            複数行の文字列。
        """
        if self.last_ratio is None:
            motion = f"MOTION --  ({self.motion.note or 'press b while nobody moves'})"
        else:
            state = "MOVING" if self.last_ratio > self.ratio_threshold else "still"
            motion = f"MOTION x{self.last_ratio:.1f} {state}  (threshold x{self.ratio_threshold:g})"
        stats = self.recorder.stats
        link = [f"ok={stats.ok}", f"bad_crc={stats.bad_crc}", f"bad_format={stats.bad_format}"]
        if snap is not None:
            now = snap.time_sec[-1]
            n_recent = int(np.sum(snap.time_sec > now - RATE_WINDOW_SEC))
            link.append(f"rate={n_recent / RATE_WINDOW_SEC:.0f} Hz")
            link.append(f"RSSI={snap.rssi_dbm[-n_recent:].mean():.1f} dBm")
            try:
                missing = io.count_missing_by_seq(snap.seq)
            except ValueError as e:
                link.append(f"seq: {e}")
            else:
                if missing is not None:
                    link.append(f"missing_seq={missing}/{len(snap.seq)}")
        if self.buffer.unsupported:
            link.append(f"unsupported={self.buffer.unsupported}")
        if self.buffer.tx is not None:
            link.append(f"tx={tx_label(self.buffer.tx, self.recorder.tx_names)}")
        if self.buffer.other_tx:
            link.append(f"other_tx={self.buffer.other_tx}")
        link += ["\n" + line for line in link_status_lines(self.buffer, {self.buffer.tx},
                                                            self.recorder.tx_names)]
        notes = [n for n in (receive_note(self.buffer, self.status),) if n]
        if self.hopper is not None and self.hopper.enabled:
            notes.append(self.hopper.describe())
        notes.append(("REC  " if self.recorder.recording else "")
                     + "keys: b=baseline 1=static 2=walk 3=empty 0=mark"
                     + channel_key_hint(self.hopper))
        return "\n".join([f"{self.title}   {motion}", "  ".join(link), "  ".join(notes)])


class CompareView:
    """2 台の送信機を左右に並べる画面（``--compare``）.

    上段が正規化振幅のウォーターフォール、下段が基準からのずれ（赤青）。左が主の送信機、
    右がもう 1 台。
    列の見出しに各リンクの動きの倍率、画面上部に送信機ごとの受信状態を出す。人が動いた時に
    2 つのリンクがそれぞれどう反応するかを見比べる（実験03 の複数リンクの考え方）。
    """

    def __init__(self, buffers: list[LiveBuffer], recorder: Recorder, status: dict[str, str],
                 title: str, ratio_threshold: float = DEFAULT_RATIO_THRESHOLD,
                 hopper: ChannelHopper | None = None) -> None:
        """画面を組み立てる.

        Args:
            buffers: 左と右のバッファ（2 つ。同じ区間）。
            recorder: 集計と印の書き出し。
            status: 受信スレッドの状態。
            title: 画面上部に出す説明。
            ratio_threshold: 「動きあり」とみなす倍率。
            hopper: チャンネルの切り替え（None なら c キーは効かない）。

        Raises:
            ValueError: バッファが 2 つでない場合。
        """
        if len(buffers) != COMPARE_LINKS:
            raise ValueError(f"バッファは {COMPARE_LINKS} つ必要です: {len(buffers)}")
        self.buffers = buffers
        self.recorder = recorder
        self.status = status
        self.title = title
        self.ratio_threshold = ratio_threshold
        self.hopper = hopper
        self.motions = [MotionTracker() for _ in buffers]
        self._channel_drawn = [0 for _ in buffers]  # バッファごとに描いたチャンネルの切り替えの数
        self.marks: list[tuple[float, str]] = []
        self.fig = plt.figure(figsize=(14, 9), layout="constrained")
        self.text = self.fig.suptitle(title, family=plotting.family_with_japanese("monospace"),
                                      fontsize=10)
        grid = self.fig.add_gridspec(2, COMPARE_LINKS)
        self.ax_wf = [self.fig.add_subplot(grid[0, i]) for i in range(COMPARE_LINKS)]
        self.ax_dev = [self.fig.add_subplot(grid[1, i]) for i in range(COMPARE_LINKS)]
        self.images, self.images_dev = [], []
        for i, buf in enumerate(buffers):
            sc = buf.subcarrier_index
            extent = (0.0, 1.0, float(sc[0]), float(sc[-1]))
            blank = np.zeros((len(sc), 2))
            img = self.ax_wf[i].imshow(blank, aspect="auto", origin="lower", cmap="viridis",
                                       extent=extent)
            dev = self.ax_dev[i].imshow(blank, aspect="auto", origin="lower", cmap="RdBu_r",
                                        vmin=-DEVIATION_CLIM, vmax=DEVIATION_CLIM, extent=extent)
            self.fig.colorbar(img, ax=self.ax_wf[i])
            self.fig.colorbar(dev, ax=self.ax_dev[i])
            self.ax_wf[i].set(xlabel="time [s]", ylabel="subcarrier index")
            self.ax_dev[i].set(xlabel="time [s]", ylabel="subcarrier index")
            self.images.append(img)
            self.images_dev.append(dev)
        self.fig.canvas.mpl_connect("key_press_event", self.on_key)

    def on_key(self, event: object) -> None:
        """``b`` で両方の静止基準を登録し、数字キーで印を付ける.

        Args:
            event: matplotlib の KeyEvent（``key`` 属性を使う）。
        """
        key = str(getattr(event, "key", ""))
        if key == BASELINE_KEY:
            self.set_baseline()
            return
        if key == CHANNEL_KEY and self.hopper is not None:
            self.hopper.request()
            return
        label = MARK_KEYS.get(key)
        snap = self.buffers[0].snapshot()
        if label is not None and snap is not None:
            self.add_mark(float(snap.time_sec[-1]), label)

    def add_mark(self, time_sec: float, label: str) -> None:
        """印を記録し、4 枚すべてに縦線を引く（時刻は左のバッファの時間軸）.

        2 つのバッファは受信の始まりが少し違うので、右の線は左の時刻を右の時間軸に合わせて引く。

        Args:
            time_sec: 左のバッファの時刻 [sec]。
            label: 印の種類。
        """
        print(self.recorder.mark(label), file=sys.stderr)
        self.marks.append((time_sec, label))
        for i in range(len(self.buffers)):
            t = self._to_local(time_sec, i)
            if t is None:
                continue
            for ax in (self.ax_wf[i], self.ax_dev[i]):
                ax.axvline(t, color=MARK_COLORS[label], lw=1.5, alpha=0.9)

    def _to_local(self, left_time: float, i: int) -> float | None:
        """左のバッファの時刻を、i 番目のバッファの時刻に直す（今からの差で合わせる）."""
        left, other = self.buffers[0].snapshot(), self.buffers[i].snapshot()
        if left is None or other is None:
            return None
        return float(other.time_sec[-1] - (left.time_sec[-1] - left_time))

    def set_baseline(self) -> bool:
        """両方のリンクの直近 ``BASELINE_SEC`` 秒を静止基準にする.

        Returns:
            両方とも登録できたか。
        """
        ok = True
        for buf, motion in zip(self.buffers, self.motions, strict=True):
            snap = buf.snapshot()
            if snap is None:
                ok = False
                continue
            t, norm, channel = normalized(snap)
            ok = motion.set_baseline(t, norm, channel, float(snap.time_sec[-1])) and ok
        left = self.buffers[0].snapshot()
        if ok and left is not None:
            self.add_mark(float(left.time_sec[-1]), "baseline")
        return ok

    def update(self) -> None:
        """両方のバッファの最新の中身で描き直す."""
        if self.hopper is not None:
            self.hopper.tick()
        any_moving = False
        for i, (buf, motion) in enumerate(zip(self.buffers, self.motions, strict=True)):
            name = tx_label(buf.tx, self.recorder.tx_names)
            snap = buf.snapshot()
            if snap is None:
                self.ax_wf[i].set_title(f"{name}: waiting for CSI")
                continue
            now = float(snap.time_sec[-1])
            t_ok, norm, ch_ok = normalized(snap)
            motion.update(norm, ch_ok, buf.silent_sec())
            recent = t_ok >= now - WATERFALL_SEC
            if recent.sum() >= 2:
                wf = norm[recent]
                sc = buf.subcarrier_index
                grid = resample_to_grid(t_ok[recent], wf, now)  # 途切れは空白
                extent = (now - WATERFALL_SEC, now, float(sc[0]), float(sc[-1]))
                self.images[i].set_data(grid.T)
                self.images[i].set_extent(extent)
                self.images[i].set_clim(*np.percentile(wf, CLIM_PERCENTILES))
                dev = wf - motion.reference(wf, ch_ok[recent])  # 基準は同じチャンネルの平均
                self.images_dev[i].set_data(resample_to_grid(t_ok[recent], dev, now).T)
                self.images_dev[i].set_extent(extent)
                for ax in (self.ax_wf[i], self.ax_dev[i]):
                    ax.set_xlim(now - WATERFALL_SEC, now)
            self._channel_drawn[i] = draw_channel_changes(
                buf, [self.ax_wf[i], self.ax_dev[i]], self._channel_drawn[i])
            moving = motion.last_ratio is not None and motion.last_ratio > self.ratio_threshold
            any_moving = any_moving or moving
            side = "left" if i == 0 else "right"
            self.ax_wf[i].set_title(f"{side}: {name}  {motion.describe(self.ratio_threshold)}",
                                    color="tab:red" if moving else "black")
            ref_name = "static baseline" if motion.baseline is not None else "mean of last 10 s"
            self.ax_dev[i].set_title(f"deviation from {ref_name}")
        self.text.set_text(self.status_text())
        self.text.set_color("tab:red" if any_moving else "black")

    def status_text(self) -> str:
        """画面上部の文（題・受信の集計・送信機ごとの受信状態・案内）."""
        stats = self.recorder.stats
        head = (f"{self.title}   ok={stats.ok} bad_crc={stats.bad_crc} "
                f"bad_format={stats.bad_format}  threshold x{self.ratio_threshold:g}")
        lines = [head, *link_status_lines(self.buffers[0], {b.tx for b in self.buffers},
                                          self.recorder.tx_names)]
        if self.hopper is not None and self.hopper.enabled:
            lines.append(self.hopper.describe())
        note = receive_note(self.buffers[0], self.status)
        keys = ("REC  " if self.recorder.recording else "") + \
            "keys: b=baseline (both) 1=static 2=walk 3=empty 0=mark" + \
            channel_key_hint(self.hopper)
        lines.append(f"{note}  {keys}" if note else keys)
        return "\n".join(lines)


def run_view(view: LiveView | CompareView, duration_sec: float | None) -> None:
    """画面を回す。対話的なバックエンドならウィンドウ、そうでなければ一定時間だけ更新する.

    Args:
        view: 画面。
        duration_sec: 自動で終える時間 [sec]（None ならウィンドウを閉じるまで）。
    """
    backend = plt.get_backend().lower()
    if backend in NON_INTERACTIVE_BACKENDS:
        if duration_sec is None:
            raise ValueError(f"バックエンド {backend} では画面を出せません。--duration を指定")
        end = time.monotonic() + duration_sec
        while time.monotonic() < end:
            time.sleep(UPDATE_INTERVAL_MS / 1000)
            view.update()
        return
    anim = FuncAnimation(view.fig, lambda _: view.update(), interval=UPDATE_INTERVAL_MS,
                         cache_frame_data=False)
    if duration_sec is not None:
        timer = view.fig.canvas.new_timer(interval=int(duration_sec * 1000))
        timer.single_shot = True
        timer.add_callback(plt.close, view.fig)
        timer.start()
    plt.show()
    del anim


def main(argv: list[str] | None = None) -> int:
    """コマンドラインから起動する.

    Args:
        argv: 引数（省略時は ``sys.argv``）。

    Returns:
        終了コード（0: 成功 / 1: 失敗）。
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", default=cap.DEFAULT_PORT)
    parser.add_argument("--baud", type=int, default=cap.DEFAULT_BAUD)
    parser.add_argument("--record", metavar="LABEL",
                        help="見ながら記録する（capture_csi と同じ形式）")
    parser.add_argument("--out-dir", type=Path, default=cap.DEFAULT_OUT_DIR)
    parser.add_argument("--no-reset", action="store_true", help="受信機をリセットしない")
    parser.add_argument("--replay", type=Path, help="シリアルの代わりに記録ファイルを再生する")
    parser.add_argument("--speed", type=float, default=1.0, help="再生速度の倍率（--replay 用）")
    parser.add_argument("--tx", help="表示する送信機の MAC（既定: 見出しの主の送信機）")
    parser.add_argument("--compare", action="store_true",
                        help="2 台の送信機を左右に並べる（左 = --tx、右 = --tx2 か、もう 1 台）")
    parser.add_argument("--tx2", help="--compare の右に出す送信機の MAC（既定: 左でない方）")
    parser.add_argument("--ltf", choices=("htltf", "lltf"), default="htltf",
                        help="取り出す区間（11g の送信機 = Pico W は lltf）")
    parser.add_argument("--ratio-threshold", type=float, default=DEFAULT_RATIO_THRESHOLD,
                        help="「動きあり」とみなす倍率（静止基準に対する比）")
    parser.add_argument("--hop", metavar="CH,CH,...",
                        help="チャンネルを自動で回す（例: 11,6。S にも無線で伝える。"
                             "c キーの順にも使う）")
    parser.add_argument("--dwell", type=float, default=DEFAULT_DWELL_SEC,
                        help=f"--hop で 1 つのチャンネルにいる時間 [sec]（{MIN_DWELL_SEC:g} 以上）")
    parser.add_argument("--picow-port", default=DEFAULT_PICOW_PORT_MODE,
                        help="USB で R に合わせる Pico W のポート（既定 none: R が無線で"
                             "切り替える。古い R のファームウェアでは auto: "
                             f"{DEFAULT_PICOW_PORT} を WHO で確かめて使う）")
    parser.add_argument("--duration", type=float, help="自動で終える時間 [sec]")
    parser.add_argument("--save-figure", type=Path, help="終了時の画面を保存する")
    cap.add_meta_arguments(parser)
    args = parser.parse_args(argv)
    plotting.use_japanese_font()  # 見出しの状態メッセージ（日本語）が □ にならないように

    if args.ratio_threshold <= 0:
        print(f"--ratio-threshold は正の値: {args.ratio_threshold}", file=sys.stderr)
        return 1
    if args.duration is not None and args.duration <= 0:
        print(f"--duration は正の値: {args.duration}", file=sys.stderr)
        return 1
    if args.replay is not None and args.record is not None:
        print("--replay と --record は同時に使えません", file=sys.stderr)
        return 1
    if args.replay is not None and not args.replay.is_file():
        print(f"再生するファイルがありません: {args.replay}", file=sys.stderr)
        return 1
    if args.replay is None and ports.port_missing(args.port):
        print(f"ポートがありません: {args.port}（USB の共有を確認）", file=sys.stderr)
        return 1
    path = None
    if args.record is not None:
        if (problem := cap.check_meta_base(args.meta_base)) is not None:
            print(problem, file=sys.stderr)
            return 1
        try:
            path = cap.build_output_path(args.out_dir, args.record, datetime.now())
        except (ValueError, FileExistsError) as e:
            print(e, file=sys.stderr)
            return 1

    channels: list[int] = list(DEFAULT_KEY_CHANNELS)
    if args.hop is not None:
        if args.replay is not None:
            print("--hop は --replay と一緒に使えません（切り替える受信機がない）", file=sys.stderr)
            return 1
        try:
            channels = [int(c) for c in args.hop.split(",") if c.strip()]
        except ValueError:
            print(f"--hop はチャンネル番号をカンマで区切る（例: 11,6）: {args.hop}",
                  file=sys.stderr)
            return 1
        if len(channels) < 2:
            print(f"--hop には 2 つ以上のチャンネルが要ります: {args.hop}", file=sys.stderr)
            return 1

    stop = threading.Event()
    status: dict[str, str] = {}
    if args.tx2 is not None and not args.compare:
        print("--tx2 は --compare と一緒に使う", file=sys.stderr)
        return 1
    try:
        buffer = LiveBuffer(tx=args.tx, ltf=args.ltf)
        buffers = [buffer]
        if args.compare:
            buffers.append(LiveBuffer(tx=args.tx2, ltf=args.ltf, peers=[buffer]))
            buffer.peers = [buffers[1]]
    except ValueError as e:
        print(f"--tx / --tx2: {e}", file=sys.stderr)
        return 1
    ser = None
    try:
        if args.replay is not None:
            chunks = replay_chunks(args.replay, stop, args.speed)
            title = f"replay: {args.replay.name}"
        else:
            ser = serial.Serial(args.port, args.baud, timeout=cap.READ_TIMEOUT_SEC)
            chunks = serial_chunks(ser, reset=not args.no_reset, stop=stop)
            title = f"live: {args.port}" + (f"  REC → {path.name}" if path else "")
    except serial.SerialException as e:
        print(f"シリアルを開けません: {e}", file=sys.stderr)
        return 1

    out = None
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        out = path.open("w", encoding="utf-8")
        out.write(f"# capture start={datetime.now().isoformat(timespec='seconds')} "
                  f"port={args.port} baud={args.baud} duration_sec=live "
                  f"reset={not args.no_reset} tool=live_csi\n")
        print(f"記録中 → {path}", file=sys.stderr)
    picow_ser = None
    followers: list[Follower] = []
    if ser is not None and args.picow_port != "none":
        port = DEFAULT_PICOW_PORT if args.picow_port == "auto" else args.picow_port
        if not ports.port_missing(port):
            picow_ser = open_picow(port)
        elif args.picow_port != "auto":
            print(f"Pico W のポートがありません: {port}", file=sys.stderr)
        if picow_ser is not None:
            followers.append(Follower("PicoW", picow_ser.write))
            print(f"Pico W（{port}）のチャンネルも R に合わせます", file=sys.stderr)
    try:
        hopper = ChannelHopper(ser.write if ser is not None else None, channels,
                               args.dwell if args.hop is not None else None,
                               followers=followers)
    except ValueError as e:
        print(f"--hop / --dwell: {e}", file=sys.stderr)
        for s_ in (ser, picow_ser):
            if s_ is not None:
                s_.close()
        return 1
    follower_threads = [threading.Thread(target=follower_loop, args=(picow_ser, hopper, f, stop),
                                         daemon=True) for f in followers if picow_ser is not None]
    for th in follower_threads:
        th.start()
    if picow_ser is not None:
        try:
            picow_ser.write(b"WHO\n")  # 読むスレッドが今のチャンネル（終了時に戻す先）を知る
        except (serial.SerialException, OSError) as e:
            print(f"Pico W に WHO を送れません: {e}", file=sys.stderr)
    recorder = Recorder(out)
    end_message = "replay finished" if args.replay is not None else None
    reader = threading.Thread(target=read_loop,
                              args=(chunks, recorder, buffers, status, end_message, hopper),
                              daemon=True)
    reader.start()
    view: LiveView | CompareView
    if args.compare:
        view = CompareView(buffers, recorder, status, title, args.ratio_threshold, hopper)
    else:
        view = LiveView(buffer, recorder, status, title, args.ratio_threshold, hopper)
    try:
        run_view(view, args.duration)
    except KeyboardInterrupt:
        print("中断しました", file=sys.stderr)
    finally:
        stop.set()
        reader.join(timeout=2.0)
        hopper.dwell_sec = None  # 閉じた後の描き直し（--save-figure）で切り替えを送らない
        for th in follower_threads:
            th.join(timeout=2.0)
        if ser is not None:
            restore_channel(ser, hopper)
            ser.close()
        if picow_ser is not None:
            for follower in followers:
                restore_follower(picow_ser, hopper, follower)
            picow_ser.close()
        if args.save_figure:
            view.update()
            args.save_figure.parent.mkdir(parents=True, exist_ok=True)
            view.fig.savefig(args.save_figure, dpi=72)
            print(f"figure: {args.save_figure}", file=sys.stderr)
        recorder.close()
    print(recorder.stats.summary())
    for warning in recorder.stats.warnings():
        print(f"警告: {warning}", file=sys.stderr)
    if path is not None:
        if recorder.stats.ok > 0:
            cap.write_meta_draft(path, args)
        else:
            print("CSI を受信しなかったので、メタデータの下書きは書きません", file=sys.stderr)
        print(path)
    return 1 if "error" in status else 0


if __name__ == "__main__":
    sys.exit(main())
