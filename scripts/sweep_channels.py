"""実験05b: 送信機と受信機のチャンネルを順に切り替え、チャンネルごとに CSI を記録する.

    uv run python scripts/sweep_channels.py                          # ch1〜13 を 1 回（各 5 秒）
    uv run python scripts/sweep_channels.py --sweeps 3 --start-delay 20 \
        --meta-base data/meta/<前回の記録名>.toml

2 つのやり方がある（``--mode``）:
- ``relay``（既定）: **受信機 R1 だけを USB でつなぐ**。R1 に ``CH n`` を送ると、
  R1 が送信機 T1 に無線（ESP-NOW）で伝え、両方が切り替わったのを確かめてから ack を返す。
  T1 は電源だけあればよく、PC から離して置ける（ファームウェアは relay=1 の版が要る）
- ``direct``: T1（送信機・115200 bps）と R1（受信機・921600 bps）の両方を USB でつなぎ、
  T1 に ``CH n``、R1 に ``CHLOCAL n`` をシリアルで送る

ポートは ``WHO`` 命令への応答で見分ける（2 台とも CP2102 でシリアル番号が同じため、名前や by-id では
区別できない）。ファームウェアの命令は firmware/csi-tx/README.md・firmware/csi-rx/README.md。

1 チャンネルの手順: チャンネルを切り替えて ack を確かめる → ``--settle`` 秒の行を捨てる →
``--duration`` 秒を記録する。記録は ``capture_csi.py`` と同じ形式
（``data/raw/<日時>-<接頭辞>-s<回>-ch<NN>.csv``、``io.load_rx_csv`` で読める）で、チャンネルごとに
メタデータの下書きを書く。記録した CSI 行のチャンネル列が n と違えば数えて警告する。

失敗時の挙動:
- 引数が範囲外・ポートがない・T1 と R1 が見つからない・MAC が期待と違う
  → 何も記録せず終了（終了コード 1）
- ack が来ない → 1 回だけ送り直し、それでも来なければ掃引を中止する
  （終了コード 1。記録済みの分は残す）
- 中止・Ctrl-C・正常終了のどれでも、最後に T1 と R1 を ``--home-channel``（既定 ch11）に戻す
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

import numpy as np
import serial

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))  # 同じディレクトリの capture_csi を使う

import capture_csi as cap  # noqa: E402

from csikit import io, meta  # noqa: E402
from csikit import ports as csikit_ports  # noqa: E402

TX_BAUD = 115200  # firmware/csi-tx の SERIAL_BAUD
RX_BAUD = 921600  # firmware/csi-rx の SERIAL_BAUD
PROBE_BAUDS = (RX_BAUD, TX_BAUD)  # ポートを見分けるときに試す順
CHANNEL_MIN = 1  # 日本で OFDM に使える範囲（ファームウェアの受け付けと同じ）
CHANNEL_MAX = 13
HOME_CHANNEL = 11  # 終わったら戻すチャンネル（ファームウェアの起動時と同じ）
DEFAULT_DURATION_SEC = 5.0
DEFAULT_SETTLE_SEC = 0.5  # 切り替え直後に捨てる時間 [sec]
# ポートを開くと M5Stack Basic が再起動することがある。起動の表示が終わるまで待つ [sec]
BOOT_WAIT_SEC = 1.5
REPLY_TIMEOUT_SEC = 1.5  # 命令の応答を待つ時間 [sec]
COMMAND_RETRIES = 1  # ack が来ないときに送り直す回数
READ_TIMEOUT_SEC = 0.05
DEFAULT_LABEL_PREFIX = "exp05b"
WHO_PATTERN = re.compile(r"who role=(tx|rx) mac=([0-9a-f]{12}) channel=(\d+)"
                         r"(?:.*?tx_mac=([0-9a-f]{12}))?(?:.*?(relay=1))?")
ERR_PATTERN = re.compile(r"err cmd=(?:CH|CHLOCAL) reason=(\S+)")
RELAY_REPLY_TIMEOUT_SEC = 4.0  # 無線で伝える CH の応答を待つ時間（再送・確認で最悪 2 秒余り）
MODES = ("relay", "direct")
CSI_CHANNEL_FIELD = 5  # format=2 の CSI 行で channel の列番号（テスト用。本体は解析して見る）


class SerialLike(Protocol):
    """``serial.Serial`` のうち、このスクリプトが使う部分（テストで偽物に差し替える）."""

    def write(self, data: bytes) -> int | None:
        """バイト列を送る."""

    def read(self, size: int) -> bytes:
        """届いている分を読む（タイムアウトで空を返す）."""

    def close(self) -> None:
        """閉じる."""


Opener = Callable[[str, int], SerialLike]
LineSink = Callable[[bytes], None]


def open_serial(port: str, baud: int) -> SerialLike:
    """M5Stack Basic を再起動させずにポートを開く（``csikit.ports.open_no_reset``）.

    Args:
        port: シリアルポート。
        baud: 速度 [bit/s]。

    Returns:
        開いたポート。

    Raises:
        OSError: 開けない場合。
    """
    opened: SerialLike = csikit_ports.open_no_reset(port, baud)
    return opened


def colon_mac(hex12: str) -> str:
    """``02000000000b`` → ``02:00:00:00:00:0b``."""
    return ":".join(hex12[i:i + 2] for i in range(0, len(hex12), 2))


class Board:
    """開いたままのポートと、そこにつながった M5Stack Basic（T1 または R1）."""

    def __init__(self, ser: SerialLike, port: str, baud: int, role: str, mac: str) -> None:
        """開いたポートを受け取る.

        Args:
            ser: 開いたポート。
            port: ポート名（表示用）。
            baud: 速度 [bit/s]。
            role: ``tx`` / ``rx``。
            mac: MAC（小文字・コロン区切り）。
        """
        self.ser = ser
        self.port = port
        self.baud = baud
        self.role = role
        self.mac = mac
        self.tx_mac: str | None = None  # 受信機が命令を伝える送信機の MAC（WHO の tx_mac=）
        self.relay = False  # 受信機が無線で送信機に伝えられるか（WHO の relay=1）
        self._assembler = cap.LineAssembler()

    def drain(self, sec: float, sink: LineSink | None = None) -> None:
        """``sec`` 秒のあいだ読み、完成した行を ``sink`` に渡す（None なら捨てる）.

        Args:
            sec: 読む時間 [sec]。
            sink: 行（改行なし）を受け取る関数。
        """
        end = time.monotonic() + sec
        while time.monotonic() < end:
            for line in self._assembler.feed(self.ser.read(cap.READ_CHUNK_BYTES)):
                if sink is not None:
                    sink(line)

    def command(self, text: str, pattern: re.Pattern[str], timeout_sec: float = REPLY_TIMEOUT_SEC,
                sink: LineSink | None = None) -> re.Match[str] | None:
        """命令を送り、``pattern`` に合う応答の行を待つ.

        Args:
            text: 命令（改行は自動で付ける）。
            pattern: 待つ応答。
            timeout_sec: 待つ時間 [sec]。
            sink: 応答以外の行を受け取る関数（None なら捨てる）。

        Returns:
            合った行の Match（時間切れ・``err`` の応答なら None）。
        """
        self.ser.write((text + "\n").encode("ascii"))
        end = time.monotonic() + timeout_sec
        while time.monotonic() < end:
            for line in self._assembler.feed(self.ser.read(cap.READ_CHUNK_BYTES)):
                decoded = line.decode("ascii", errors="replace")
                if m := pattern.search(decoded):
                    return m
                if ERR_PATTERN.search(decoded):
                    print(f"  {self.role}: {decoded.strip()}", file=sys.stderr)
                    return None
                if sink is not None:
                    sink(line)
        return None

    def set_channel(self, channel: int, command: str = "CH",
                    timeout_sec: float | None = None) -> None:
        """チャンネルを切り替え、ack を確かめる（来なければ ``COMMAND_RETRIES`` 回送り直す）.

        Args:
            channel: 切り替え先。
            command: ``CH``（受信機では送信機にも伝える）か ``CHLOCAL``（受信機だけ）。
            timeout_sec: 応答を待つ時間 [sec]（None なら ``REPLY_TIMEOUT_SEC``）。

        Raises:
            RuntimeError: ack が来ない、または違うチャンネルの ack が来た場合。
        """
        pattern = re.compile(rf"ack cmd={command} channel=(\d+)")
        wait = REPLY_TIMEOUT_SEC if timeout_sec is None else timeout_sec
        for _ in range(1 + COMMAND_RETRIES):
            m = self.command(f"{command} {channel}", pattern, wait)
            if m is not None and int(m.group(1)) == channel:
                return
        raise RuntimeError(f"{self.role}（{self.port}）が {command} {channel} に応答しません")

    def close(self) -> None:
        """ポートを閉じる."""
        self.ser.close()


def probe(port: str, opener: Opener) -> Board | None:
    """ポートを速度を変えながら開き、``WHO`` の応答で M5Stack Basic を見分ける.

    Args:
        port: シリアルポート。
        opener: ポートを開く関数。

    Returns:
        見分けられた M5Stack Basic（どの速度でも応答がなければ None）。
    """
    for baud in PROBE_BAUDS:
        try:
            ser = opener(port, baud)
        except (OSError, serial.SerialException) as e:
            print(f"  {port}: 開けません: {e}", file=sys.stderr)
            return None
        board = Board(ser, port, baud, role="?", mac="?")
        board.drain(BOOT_WAIT_SEC)
        m = board.command("WHO", WHO_PATTERN)
        if m is not None:
            board.role, board.mac = m.group(1), colon_mac(m.group(2))
            board.tx_mac = colon_mac(m.group(4)) if m.group(4) else None
            board.relay = m.group(5) is not None
            return board
        ser.close()
    return None


def discover(ports: list[str], opener: Opener, tx_mac: str | None = None,
             rx_mac: str | None = None, mode: str = "direct") -> tuple[Board | None, Board]:
    """受信機（direct なら送信機も）のポートを見つける.

    Args:
        ports: 調べるポート。
        opener: ポートを開く関数。
        tx_mac: 期待する送信機の MAC（None なら照合しない）。
        rx_mac: 期待する受信機の MAC（None なら照合しない）。
        mode: ``relay``（受信機だけ使う）/ ``direct``（両方使う）。

    Returns:
        (送信機 or None, 受信機)。relay では送信機は None（見つかっても閉じる）。

    Raises:
        RuntimeError: 要る M5Stack Basic が見つからない、同じ役割が 2 台ある、MAC が期待と違う、
            relay で受信機のファームウェアが無線の命令に対応していない、受信機が組んでいる
            送信機の MAC が期待（指定が無ければ、つながっている送信機）と違う場合
            （開いたポートは閉じてから投げる）。
    """
    boards = [b for b in (probe(p, opener) for p in ports) if b is not None]
    found = {b.role: b for b in boards}
    problems = []
    if len(found) != len(boards):
        problems.append("同じ役割の M5Stack Basic が 2 台あります")
    needed = (("rx", rx_mac),) if mode == "relay" else (("tx", tx_mac), ("rx", rx_mac))
    for role, want in needed:
        board = found.get(role)
        if board is None:
            problems.append(f"{role} が見つかりません（調べたポート: {ports}）")
        elif want is not None and board.mac != want:
            problems.append(f"{role}（{board.port}）の MAC が {board.mac}（期待 {want}）")
    rx = found.get("rx")
    # 受信機の相手の照合: 指定があればそれと、無ければつながっている送信機と比べる
    want_peer = tx_mac or (found["tx"].mac if "tx" in found else None)
    if mode == "relay" and rx is not None and not rx.relay:
        problems.append("受信機のファームウェアが無線の命令（relay=1）に対応していません"
                        "（--mode direct を使うか、書き込み直す）")
    elif rx is not None and want_peer is not None and rx.tx_mac not in (None, want_peer):
        problems.append(f"受信機が組んでいる送信機の MAC が {rx.tx_mac}（期待 {want_peer}。"
                        "組ませ直すなら uv run csikit flash --pair-only）")
    if problems:
        for b in boards:
            b.close()
        raise RuntimeError("; ".join(problems))
    tx = found.get("tx")
    if mode == "relay" and tx is not None:
        tx.close()  # 使わない（電源だけでよい）
        tx = None
    assert rx is not None
    return tx, rx


@dataclass
class ChannelResult:
    """1 チャンネル分の記録の結果.

    Attributes:
        sweep: 何回目の掃引か。
        channel: チャンネル。
        path: 記録ファイル。
        stats: 行の集計。
        channel_mismatch: チャンネル列が ``channel`` と違った CSI 行の数。
    """

    sweep: int
    channel: int
    path: Path
    stats: cap.CaptureStats
    channel_mismatch: int = 0


def record(rx: Board, tx_mac: str, path: Path, channel: int, sweep: int,
           args: argparse.Namespace) -> ChannelResult:
    """受信機の行を ``args.duration`` 秒記録する（``capture_csi.py`` と同じ形式）.

    先頭の注記には受信機の起動時と同じ形の ``# mac=… channel=… tx_mac=…`` 行を書く
    （メタデータの下書きが MAC とチャンネルを読めるように）。

    Args:
        rx: 受信機。
        tx_mac: 送信機の MAC（注記に書く）。
        path: 記録ファイル。
        channel: 今のチャンネル。
        sweep: 何回目の掃引か。
        args: 引数（``duration``・``settle``・``channels``）。

    Returns:
        結果。
    """
    result = ChannelResult(sweep=sweep, channel=channel, path=path, stats=cap.CaptureStats())
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as out:
        out.write(f"# capture start={datetime.now().isoformat(timespec='seconds')} "
                  f"port={rx.port} baud={rx.baud} duration_sec={args.duration} reset=False "
                  f"tool=sweep_channels\n")
        out.write("# CSI receiver format=2\n")
        out.write(f"# mac={rx.mac.replace(':', '')} channel={channel} (want {channel}) "
                  f"tx_mac={tx_mac.replace(':', '')} baud={rx.baud} tool=sweep_channels\n")
        out.write(f"# sweep sweep={sweep} channel={channel} mode={args.mode} "
                  f"channels={','.join(map(str, args.channels))} settle_sec={args.settle}\n")

        def sink(line: bytes) -> None:
            before = result.stats.ok
            cap.handle_line(line, out, result.stats)
            if result.stats.ok > before:
                # 列の位置は形式で違う（format=3 は送信元の列が増える）ので解析して見る
                if io.parse_rx_line(line.decode("ascii")).channel != channel:
                    result.channel_mismatch += 1

        rx.drain(args.duration, sink)
        out.write(f"# summary {result.stats.summary()}\n")
    return result


def describe(res: ChannelResult) -> str:
    """結果を表の 1 行にする（送信機が 2 台なら、行数・欠番は主の送信機のもの）.

    Args:
        res: 結果。

    Returns:
        表示用の文字列。
    """
    stats = res.stats
    groups = list(stats.groups().items())
    primary_idx = groups[0][1] if groups else []
    seq = np.array([stats.seq[i] for i in primary_idx], dtype=np.int64)
    try:
        missing = io.count_missing_by_seq(seq)
    except ValueError as e:
        missing_text = f"連番の異常: {e}"
    else:
        missing_text = "?" if missing is None else str(missing)
    others = sum(len(idx) for _, idx in groups[1:])
    other_text = f"  他の送信機 {others} 行" if others else ""
    return (f"s{res.sweep} ch{res.channel:02d}: 行 {len(primary_idx):4d}  欠番 {missing_text:>4s}  "
            f"CRC不一致 {stats.bad_crc}  チャンネル不一致 {res.channel_mismatch}{other_text}  "
            f"{res.path.name}")


def switch(tx: Board | None, rx: Board, channel: int) -> None:
    """両方を ``channel`` に切り替える（relay: 受信機経由 / direct: 送信機 → 受信機の順）.

    Args:
        tx: 送信機（relay では None）。
        rx: 受信機。
        channel: 切り替え先。

    Raises:
        RuntimeError: ack が来ない場合。
    """
    if tx is None:
        rx.set_channel(channel, "CH", RELAY_REPLY_TIMEOUT_SEC)
    else:
        # 送信機 → 受信機の順（受信機が先だと古いチャンネルの行が混ざる）
        tx.set_channel(channel, "CH")
        rx.set_channel(channel, "CHLOCAL")


def sweep(tx: Board | None, rx: Board, args: argparse.Namespace,
          results: list[ChannelResult]) -> None:
    """掃引する。記録するたびに ``results`` に足す（途中で止まっても記録済みの分が残る）.

    チャンネルを戻すのは呼び出し側。

    Args:
        tx: 送信機（relay では None）。
        rx: 受信機。
        args: 引数。
        results: 結果を足していくリスト。

    Raises:
        RuntimeError: ack が来ない場合。
        FileExistsError: 同名の記録がある場合。
    """
    for k in range(1, args.sweeps + 1):
        for ch in args.channels:
            switch(tx, rx, ch)
            rx.drain(args.settle)  # 切り替え直後の行は捨てる
            label = f"{args.label_prefix}-s{k}-ch{ch:02d}"
            path = cap.build_output_path(args.out_dir, label, datetime.now())
            res = record(rx, args.tx_mac or rx.tx_mac or "", path, ch, k, args)
            results.append(res)
            print(describe(res))
            for warning in res.stats.warnings():
                print(f"  警告: {warning}", file=sys.stderr)
            if res.channel_mismatch:
                print(f"  警告: チャンネル列が {ch} でない行が {res.channel_mismatch} 行",
                      file=sys.stderr)
            cap.write_meta_draft(path, args)


def return_home(tx: Board | None, rx: Board, channel: int) -> bool:
    """両方を ``channel`` に戻す（失敗しても結果を返す）.

    Args:
        tx: 送信機（relay では None）。
        rx: 受信機。
        channel: 戻すチャンネル。

    Returns:
        戻せたか。
    """
    try:
        switch(tx, rx, channel)
    except RuntimeError as e:
        print(f"戻せませんでした: {e}（番犬で数秒後に ch{HOME_CHANNEL} に戻るはず。"
              f"戻らなければ電源を入れ直すと ch{HOME_CHANNEL} で起動します）", file=sys.stderr)
        return False
    return True


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """引数を読む.

    Args:
        argv: 引数（省略時は ``sys.argv``）。

    Returns:
        読んだ引数。
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--channels", type=int, nargs="+",
                        default=list(range(CHANNEL_MIN, CHANNEL_MAX + 1)))
    parser.add_argument("--sweeps", type=int, default=1, help="掃引の回数")
    parser.add_argument("--duration", type=float, default=DEFAULT_DURATION_SEC,
                        help="1 チャンネルの記録時間 [sec]")
    parser.add_argument("--settle", type=float, default=DEFAULT_SETTLE_SEC,
                        help="切り替え直後に捨てる時間 [sec]")
    parser.add_argument("--home-channel", type=int, default=HOME_CHANNEL,
                        help="終わったら戻すチャンネル")
    parser.add_argument("--label-prefix", default=DEFAULT_LABEL_PREFIX)
    parser.add_argument("--ports", nargs="+", help="調べるポート（既定: USB のシリアルポート全部）")
    parser.add_argument("--mode", choices=MODES, default="relay",
                        help="relay: 受信機だけつなぐ（送信機へは無線）/ direct: 両方つなぐ")
    parser.add_argument("--tx-mac", help="送信機の MAC を照合する（省略時は照合しない）")
    parser.add_argument("--rx-mac", help="受信機の MAC を照合する（省略時は照合しない）")
    parser.add_argument("--out-dir", type=Path, default=cap.DEFAULT_OUT_DIR)
    parser.add_argument("--start-delay", type=float, default=0.0,
                        help="掃引を始める前に待つ時間 [sec]（部屋を出る間の動きを入れない）")
    cap.add_meta_arguments(parser)
    parser.set_defaults(experiment="exp05b")
    return parser.parse_args(argv)


def check_args(args: argparse.Namespace) -> list[str]:
    """引数の範囲を確かめる.

    Args:
        args: 読んだ引数。

    Returns:
        問題の一覧（空なら問題なし）。
    """
    problems = []
    for ch in [*args.channels, args.home_channel]:
        if not CHANNEL_MIN <= ch <= CHANNEL_MAX:
            problems.append(f"チャンネルは {CHANNEL_MIN}〜{CHANNEL_MAX}: {ch}")
    if len(set(args.channels)) != len(args.channels):
        problems.append(f"--channels に重複があります: {args.channels}")
    if args.sweeps < 1:
        problems.append(f"--sweeps は 1 以上: {args.sweeps}")
    if args.duration <= 0:
        problems.append(f"--duration は正の値: {args.duration}")
    if args.settle < 0 or args.start_delay < 0:
        problems.append("--settle と --start-delay は 0 以上")
    if not cap.LABEL_PATTERN.fullmatch(f"{args.label_prefix}-s{args.sweeps}-ch{CHANNEL_MAX}"):
        problems.append(f"--label-prefix が長すぎるか、使えない文字を含みます: {args.label_prefix}")
    if (problem := cap.check_meta_base(args.meta_base)) is not None:
        problems.append(problem)
    return problems


def main(argv: list[str] | None = None, opener: Opener | None = None) -> int:
    """掃引する.

    Args:
        argv: 引数（省略時は ``sys.argv``）。
        opener: ポートを開く関数（テストで偽物を渡す。None なら実機）。

    Returns:
        終了コード（0: 全チャンネルを記録した / 1: それ以外）。
    """
    args = parse_args(argv)
    problems = check_args(args)
    ports = args.ports or [p.device for p in csikit_ports.list_ports()]
    if opener is None:
        opener = open_serial
        missing = [p for p in ports if csikit_ports.port_missing(p)]
        if not ports or missing:
            where = missing or "USB のシリアルポート"
            problems.append(f"ポートがありません: {where}（USB の共有を確認）")
    if problems:
        for p in problems:
            print(p, file=sys.stderr)
        return 1

    if args.meta_base is not None and (todos := meta.find_todos(meta.load_meta(args.meta_base))):
        print(f"警告: --meta-base に TODO が {len(todos)} 件あります（全記録に写ります）: "
              f"{', '.join(todos[:5])}", file=sys.stderr)
    print(f"ポートを調べています: {ports}", file=sys.stderr)
    try:
        tx, rx = discover(ports, opener, args.tx_mac, args.rx_mac, args.mode)
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1
    if tx is None:
        print(f"受信機 {rx.mac} = {rx.port}（送信機 {rx.tx_mac} へは無線）", file=sys.stderr)
    else:
        print(f"送信機 {tx.mac} = {tx.port}、受信機 {rx.mac} = {rx.port}", file=sys.stderr)

    results: list[ChannelResult] = []
    failed = False
    try:
        cap.wait_before_start(args.start_delay)
        print(f"掃引: ch{args.channels} × {args.sweeps} 回（各 {args.duration:g} 秒）",
              file=sys.stderr)
        sweep(tx, rx, args, results)
    except (RuntimeError, FileExistsError) as e:
        print(f"掃引を中止しました: {e}", file=sys.stderr)
        failed = True
    except KeyboardInterrupt:
        print("中断しました", file=sys.stderr)
        failed = True
    finally:
        home_ok = return_home(tx, rx, args.home_channel)
        if tx is not None:
            tx.close()
        rx.close()
    print(f"記録 {len(results)} 本（予定 {len(args.channels) * args.sweeps} 本）。"
          f"ch{args.home_channel} に{'戻しました' if home_ok else '戻せませんでした'}")
    return 1 if failed or not home_ok else 0


if __name__ == "__main__":
    sys.exit(main())
