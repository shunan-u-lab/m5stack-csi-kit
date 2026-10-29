"""受信機（firmware/csi-rx）のシリアル出力を data/raw/ に記録する.

    uv run python scripts/capture_csi.py --duration 60 --label empty
    uv run python scripts/capture_csi.py --duration 60 --label walk --no-reset
    uv run python scripts/capture_csi.py --duration 30 --label exp05a-p01-r1 --start-delay 20
      （--start-delay: 指定秒数待ってから記録を始める。部屋を出る間の動きを記録に入れない）

保存するのは CRC が一致した ``CSI,`` 行と、受信機の注記（``#`` 行）だけ。
記録が終わると、メタデータの下書き ``data/meta/<記録名>.toml`` を書く（自動で分かる項目を埋め、
人が書く項目は TODO。``--meta-base`` で前回の配置などを写す。書式は docs/decisions/0003）。
CRC 不一致の行（USB 転送で壊れた行）と、形式の合わない行（受信機とパーサの版違い）は
別々に数えて捨て、最後に集計を表示・追記する。
読み込みは ``csikit.io.load_rx_csv``。

``--no-reset`` のときは、DTR/RTS を下げて開き（受信機を再起動しない。SD に記録中の
セッションも切らない）、途中の行を捨ててから ``WHO`` を送る。応答
（``# who role=rx mac=… channel=… tx_mac=…``）が記録に残るので、リセットしたときの起動時の
表示と同じく、メタデータの下書きに MAC とチャンネルが入る。

失敗時の挙動:
- ポートが無い・ラベルが不正・同名ファイルがある → 何も書かずに終了（終了コード 1）
- 開始から ``NO_DATA_TIMEOUT_SEC`` 秒たっても CSI 行が 1 つも来ない → 原因の候補を示して
  終了（終了コード 1）。空のファイルは消す
- Ctrl-C → それまでの記録を残し、集計して終了（``--start-delay`` の待ち時間中なら何も書かずに終了）
- メタデータの下書きを書けない（同名がある等）→ 警告だけ出す。記録は残す（終了コード 0）
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

import numpy as np
import serial

from csikit import io, meta, ports

DEFAULT_PORT = "/dev/ttyUSB0"
DEFAULT_BAUD = 921600  # firmware/csi-rx の SERIAL_BAUD
DEFAULT_OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"
DEFAULT_META_DIR = Path(__file__).resolve().parent.parent / "data" / "meta"
TX_INTERVAL_US = 10_000  # 送信機の送信間隔 [µs]（firmware/csi-tx の SEND_INTERVAL_MS）
READ_CHUNK_BYTES = 65536
WHO_COMMAND = b"WHO\n"  # リセットしない記録で、受信機の MAC・チャンネルを記録に残すため
READ_TIMEOUT_SEC = 0.2
RESET_PULSE_SEC = 0.1
STALE_FLUSH_SEC = 0.05  # リセットを解いてから、届き残りのリセット前の行を捨てるまでの時間 [sec]
NO_DATA_TIMEOUT_SEC = 5.0
COUNTDOWN_STEP_SEC = 5.0  # --start-delay の残り時間を表示する間隔
MAX_BAD_RATIO = 0.01  # CRC 不一致がこれを超えたら警告
MIN_RATE_RATIO = 0.9  # 実効レートが送信レートのこの割合を下回ったら警告
LABEL_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,40}")
TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"
DROPPED_PATTERN = re.compile(r"\bdropped=(\d+)\b")


class LineAssembler:
    """読み取った断片を行に組み立てる（行が断片の境目で切れても正しく扱う）."""

    def __init__(self) -> None:
        """空の状態で始める."""
        self._pending = b""

    def feed(self, chunk: bytes) -> list[bytes]:
        """断片を受け取り、完成した行（改行を除く）を返す.

        Args:
            chunk: シリアルから読んだバイト列。

        Returns:
            完成した行のリスト。最後の未完成の行は次回に持ち越す。
        """
        data = self._pending + chunk
        *lines, self._pending = data.split(b"\n")
        return [line.rstrip(b"\r") for line in lines]


@dataclass
class CaptureStats:
    """記録中の集計.

    送信機が 2 台以上の記録（format=3）では、レート・欠番を送信機ごとに数える（2 台の連番が混ざると
    欠番の計算が壊れるため）。代表の値（``rate_hz``・``est_missing``・``missing_seq``）は、受信機の見出しに
    ある主の送信機（``# tx id=0 mac=…``）のもの。見出しがなければ行の多い送信機のもの。
    """

    ok: int = 0
    bad_crc: int = 0
    bad_format: int = 0  # CRC は正しいが解析できない（受信機の format とパーサの版違い等）
    seq: list[int] = field(default_factory=list)
    other: int = 0  # 起動ログ等、CSI でも注記でもない行
    rx_us: list[int] = field(default_factory=list)
    tx: list[str | None] = field(default_factory=list)  # 行ごとの送信元（format=3 より前は None）
    last_receiver_stats: str = ""
    primary_tx: str | None = None  # 受信機の見出しにある主の送信機

    def groups(self) -> dict[str | None, list[int]]:
        """送信元 → 行の添字（主の送信機が先頭、あとは行の多い順）。不明なら全体で 1 つ."""
        if len(self.tx) != len(self.rx_us):
            return {None: list(range(len(self.rx_us)))}
        out: dict[str | None, list[int]] = {}
        for i, t in enumerate(self.tx):
            out.setdefault(t, []).append(i)
        return dict(sorted(out.items(),
                           key=lambda kv: (kv[0] != self.primary_tx, -len(kv[1]))))

    def _timing(self, idx: list[int]) -> tuple[float, float, int] | None:
        """添字の行の (記録時間 [sec], レート [Hz], 推定した欠落) を返す（2 行未満は None）."""
        if len(idx) < 2:
            return None
        elapsed_us = io.unwrap_rx_us(np.array([self.rx_us[i] for i in idx], dtype=np.int64))
        span_sec = float(elapsed_us[-1]) / 1e6
        rate_hz = (len(idx) - 1) / span_sec if span_sec > 0 else 0.0
        return span_sec, rate_hz, io.estimate_missing(elapsed_us, TX_INTERVAL_US)

    def _seq_parts(self, idx: list[int], prefix: str) -> list[str]:
        """添字の行の連番から、不明の数・欠番・送信機の再起動の回数を数える.

        送信機が再起動すると連番が 0 に戻るので、戻った所で区切って区切りごとに欠番を数える
        （tx_restarts）。記録ファイルにも書くので、出力は ASCII だけにする（2026-10-07 に日本語の
        エラー文を書いて、解析が ASCII として読めずに止まった）。
        """
        seq = np.array([self.seq[i] for i in idx if i < len(self.seq)], dtype=np.int64)
        parts = [f"{prefix}seq_unknown={int((seq == io.SEQ_UNKNOWN).sum())}"]
        known = seq[seq != io.SEQ_UNKNOWN]
        runs = np.split(known, np.where(np.diff(known) < 0)[0] + 1) if len(known) else []
        missing = 0
        for run in runs:
            try:
                m = io.count_missing_by_seq(run)
            except ValueError:
                parts.append(f"{prefix}seq_error=duplicate")
                return parts
            missing += m or 0
        if len(known) >= 2:
            parts.append(f"{prefix}missing_seq={missing}")
        if len(runs) > 1:
            parts.append(f"{prefix}tx_restarts={len(runs) - 1}")
        return parts

    def summary(self) -> str:
        """集計を 1 行にまとめる."""
        total = self.ok + self.bad_crc
        bad_ratio = self.bad_crc / total if total else 0.0
        parts = [f"ok={self.ok}", f"bad_crc={self.bad_crc}", f"bad_ratio={bad_ratio:.4f}",
                 f"bad_format={self.bad_format}", f"other={self.other}"]
        groups = self.groups()
        primary = next(iter(groups.values()), [])
        if (timing := self._timing(primary)) is not None:
            span_sec, rate_hz, missing = timing
            parts += [f"span_sec={span_sec:.2f}", f"rate_hz={rate_hz:.1f}",
                      f"est_missing={missing}"]
        parts += self._seq_parts(primary, "")
        if len(groups) > 1:
            for name, idx in groups.items():
                key = f"tx_{(name or 'unknown').replace(':', '')}_"
                parts.append(f"{key}ok={len(idx)}")
                if (timing := self._timing(idx)) is not None:
                    parts.append(f"{key}rate_hz={timing[1]:.1f}")
                parts += self._seq_parts(idx, key)
        return " ".join(parts)

    def warnings(self) -> list[str]:
        """品質の問題を列挙する."""
        found = []
        total = self.ok + self.bad_crc
        if total and self.bad_crc / total > MAX_BAD_RATIO:
            found.append(f"CRC 不一致が {self.bad_crc}/{total} 行。USB の接続を確認してください")
        if self.bad_format:
            found.append(
                f"形式の合わない行が {self.bad_format} 行。受信機の format とパーサの版を確認"
            )
        expected_hz = 1e6 / TX_INTERVAL_US
        for name, idx in self.groups().items():
            timing = self._timing(idx)
            if timing is not None and timing[0] > 0 and timing[1] < MIN_RATE_RATIO * expected_hz:
                who = f"送信機 {name} の" if name else ""
                found.append(
                    f"{who}実効レートが {expected_hz:.0f} Hz の {MIN_RATE_RATIO:.0%} 未満です")
        # 受信機の dropped は起動からの累計（--no-reset では記録前の分も含む）
        m = DROPPED_PATTERN.search(self.last_receiver_stats)
        if m and int(m.group(1)) > 0:
            found.append(f"受信機内で取りこぼしがあります: {self.last_receiver_stats}")
        return found


def handle_line(line: bytes, out: TextIO, stats: CaptureStats) -> None:
    """1 行を分類し、保存すべきものだけ書き出す.

    Args:
        line: 改行を除いた 1 行。
        out: 書き出し先。
        stats: 更新する集計。
    """
    text = line.decode("ascii", errors="replace")
    if text.startswith(io.RX_LINE_PREFIX):
        try:
            rec = io.parse_rx_line(text)
        except io.RxCrcError:
            stats.bad_crc += 1
            return
        except ValueError:
            stats.bad_format += 1
            return
        out.write(text + "\n")
        stats.ok += 1
        stats.rx_us.append(rec.rx_us)
        stats.seq.append(rec.seq)
        stats.tx.append(rec.tx)
    elif text.startswith(io.RX_COMMENT_PREFIX):
        out.write(text + "\n")
        if stats.primary_tx is None and (m := io.PRIMARY_TX_PATTERN.match(text)):
            stats.primary_tx = io.colon_mac(m.group(1) or m.group(2))
        if text.startswith("# stats"):
            stats.last_receiver_stats = text
    elif text.strip():
        stats.other += 1


def build_output_path(out_dir: Path, label: str, now: datetime) -> Path:
    """保存先のパスを決める.

    Args:
        out_dir: 保存先ディレクトリ。
        label: 実験条件のラベル（英数字・``_``・``-``）。
        now: 開始時刻。

    Returns:
        ``<out_dir>/<YYYYmmdd-HHMMSS>-<label>.csv``。

    Raises:
        ValueError: ラベルが不正な場合。
        FileExistsError: 同名のファイルが既にある場合（上書きしない）。
    """
    if not LABEL_PATTERN.fullmatch(label):
        raise ValueError(f"ラベルは英数字・_・- で 1〜40 文字: {label!r}")
    path = out_dir / f"{now.strftime(TIMESTAMP_FORMAT)}-{label}.csv"
    if path.exists():
        raise FileExistsError(f"既にあります（上書きしません）: {path}")
    return path


def reset_receiver(ser: Any, sleep: Callable[[float], None] = time.sleep) -> None:
    """受信機をリセットし、リセット前にたまった行を捨てる（live_csi.py と共通）.

    捨てずに残すと、受信時刻がリセットで 0 に戻るのを 32bit の一周と取り違え、区間が約 4295 秒に
    延びる（live_csi は 2026-09-23 に対策済み。capture_csi は 2026-10-06 の実験10 で 2 本起きた）。
    リセット中に 1 回、解いた直後に USB の途中にあった行のためにもう 1 回捨てる
    （解いた直後に出るのは ROM の起動表示だけ）。

    Args:
        ser: 開いたシリアルポート（``setDTR`` / ``setRTS`` / ``reset_input_buffer`` を持つ）。
        sleep: 待つ関数（テストで差し替える）。
    """
    ser.setDTR(False)
    ser.setRTS(True)
    sleep(RESET_PULSE_SEC)
    ser.reset_input_buffer()
    ser.setRTS(False)
    sleep(STALE_FLUSH_SEC)
    ser.reset_input_buffer()


def open_for_capture(port: str, baud: int, reset: bool) -> Any:
    """記録用にポートを開く.

    reset なら普通に開く（後で ``reset_receiver`` でリセットする）。そうでなければ DTR/RTS を
    下げて開き、途中の行を捨ててから WHO を送る（応答は記録の先頭近くに ``#`` 行として残る）。
    """
    if reset:
        return serial.Serial(port, baud, timeout=READ_TIMEOUT_SEC)
    ser = ports.open_no_reset(port, baud)
    ports.flush_partial_line(ser)
    ser.write(WHO_COMMAND)
    return ser


def capture(port: str, baud: int, duration_sec: float, path: Path, reset: bool,
            opener: Callable[[str, int, bool], Any] = open_for_capture,
            tx_fw: str | None = None) -> CaptureStats:
    """シリアルを読み、``path`` に記録する.

    Args:
        port: シリアルポート。
        baud: ボーレート [bit/s]。
        duration_sec: 記録時間 [sec]。
        path: 保存先。
        reset: 開始時に受信機をリセットするか（起動時の設定行も記録に残る）。
            しないときは WHO の応答を記録に残す。
        opener: ポートを開く関数（テスト用）。
        tx_fw: 送信機のファームの版。渡せば ``# capture tx_fw=…`` の行を記録に残す
            （メタデータの下書きが読む。受信機は送信機の版を知らないため）。

    Returns:
        集計。

    Raises:
        TimeoutError: ``NO_DATA_TIMEOUT_SEC`` 秒たっても CSI 行が来ない場合。
    """
    stats = CaptureStats()
    assembler = LineAssembler()
    path.parent.mkdir(parents=True, exist_ok=True)
    with (opener(port, baud, reset) as ser,
          path.open("w", encoding="utf-8") as out):
        out.write(f"# capture start={datetime.now().isoformat(timespec='seconds')} "
                  f"port={port} baud={baud} duration_sec={duration_sec} reset={reset}\n")
        if tx_fw is not None and (fw_line := meta.tx_fw_line(tx_fw)) is not None:
            out.write(fw_line + "\n")
        if reset:
            reset_receiver(ser)
        started = time.monotonic()
        try:
            while time.monotonic() - started < duration_sec:
                for line in assembler.feed(ser.read(READ_CHUNK_BYTES)):
                    handle_line(line, out, stats)
                out.flush()
                if stats.ok == 0 and time.monotonic() - started > NO_DATA_TIMEOUT_SEC:
                    raise TimeoutError(
                        f"{NO_DATA_TIMEOUT_SEC:.0f} 秒間 CSI 行が来ません。確認: 送信機の電源、"
                        f"受信機が firmware/csi-rx か、ボーレート {baud}、チャンネルの一致"
                        f"（受信した行: CRC 不一致 {stats.bad_crc}・形式不一致 {stats.bad_format}"
                        f"・その他 {stats.other}。形式不一致が多ければ受信機とパーサの版違い）"
                    )
        except KeyboardInterrupt:
            print("中断しました。ここまでの記録を残します", file=sys.stderr)
        out.write(f"# summary {stats.summary()}\n")
    return stats


def wait_before_start(delay_sec: float, sleep: Callable[[float], None] = time.sleep,
                      out: TextIO = sys.stderr) -> None:
    """記録を始める前に待つ（残り時間を ``COUNTDOWN_STEP_SEC`` ごとに表示する）.

    Args:
        delay_sec: 待つ時間 [sec]（0 なら待たない）。
        sleep: 待つ関数（テストで差し替える）。
        out: 残り時間の表示先。
    """
    remaining = delay_sec
    while remaining > 0:
        print(f"記録開始まで {remaining:.0f} 秒（手順書の位置について、動かずに待つ）", file=out)
        step = min(COUNTDOWN_STEP_SEC, remaining)
        sleep(step)
        remaining -= step


def add_meta_arguments(parser: argparse.ArgumentParser) -> None:
    """メタデータの下書きに関する引数を足す（live_csi.py と共通）.

    Args:
        parser: 引数の定義先。
    """
    parser.add_argument("--experiment", default=meta.TODO, help="実験の番号（例: exp04）")
    parser.add_argument("--meta-base", type=Path,
                        help="配置などを写す前回のメタデータ（data/meta/<記録名>.toml）")
    parser.add_argument("--meta-dir", type=Path, default=DEFAULT_META_DIR)
    parser.add_argument("--no-meta", action="store_true", help="メタデータの下書きを書かない")


def check_meta_base(path: Path | None) -> str | None:
    """``--meta-base`` を記録の前に確かめる（記録の後で失敗しないように）.

    Args:
        path: 前回のメタデータ（None なら確かめない）。

    Returns:
        問題の説明（問題がなければ None）。
    """
    if path is None:
        return None
    try:
        problems = meta.validate_meta(meta.load_meta(path))
    except (OSError, ValueError) as e:
        return f"--meta-base を読めません: {e}"
    if problems:
        return f"--meta-base に形の問題があります: {path}: " + "; ".join(problems)
    return None


def write_meta_draft(csv_path: Path, args: argparse.Namespace) -> Path | None:
    """記録の後にメタデータの下書きを書く。失敗しても記録は残すので、警告だけ出す.

    Args:
        csv_path: 記録ファイル。
        args: ``add_meta_arguments`` で足した引数を含む。

    Returns:
        書いたファイル（書かなかった・書けなかった場合は None）。
    """
    if args.no_meta:
        return None
    try:
        out = meta.write_draft(csv_path, args.meta_dir, args.experiment, args.meta_base)
    except (OSError, ValueError) as e:
        print(f"警告: メタデータの下書きを書けませんでした: {e}", file=sys.stderr)
        return None
    todos = meta.find_todos(meta.load_meta(out))
    print(f"メタデータの下書き: {out}（TODO {len(todos)} 件を埋めてください）", file=sys.stderr)
    return out


def main(argv: list[str] | None = None) -> int:
    """コマンドラインから記録する.

    Args:
        argv: 引数（省略時は ``sys.argv``）。

    Returns:
        終了コード（0: 成功 / 1: 失敗）。
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--duration", type=float, required=True, help="記録時間 [sec]")
    parser.add_argument("--label", required=True, help="実験条件（例: empty, walk）")
    parser.add_argument("--port", default=DEFAULT_PORT)
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--no-reset", action="store_true", help="受信機をリセットしない")
    parser.add_argument("--start-delay", type=float, default=0.0,
                        help="記録を始める前に待つ時間 [sec]（部屋を出る間の動きを入れない）")
    parser.add_argument("--tx-fw",
                        help="送信機のファームの版（csikit record が WHO で調べて渡す）")
    add_meta_arguments(parser)
    args = parser.parse_args(argv)

    if args.duration <= 0:
        print(f"--duration は正の値: {args.duration}", file=sys.stderr)
        return 1
    if args.start_delay < 0:
        print(f"--start-delay は 0 以上: {args.start_delay}", file=sys.stderr)
        return 1
    if args.tx_fw is not None and meta.tx_fw_line(args.tx_fw) is None:
        print(f"--tx-fw が版の形ではありません: {args.tx_fw!r}", file=sys.stderr)
        return 1
    if ports.port_missing(args.port):
        print(f"ポートがありません: {args.port}（USB の共有を確認）", file=sys.stderr)
        return 1
    if (problem := check_meta_base(args.meta_base)) is not None:
        print(problem, file=sys.stderr)
        return 1
    try:
        wait_before_start(args.start_delay)
    except KeyboardInterrupt:
        print("待ち時間中に中断しました（何も記録していません）", file=sys.stderr)
        return 1
    try:
        path = build_output_path(args.out_dir, args.label, datetime.now())
    except (ValueError, FileExistsError) as e:
        print(e, file=sys.stderr)
        return 1

    print(f"記録中 {args.duration:.0f} 秒 → {path}", file=sys.stderr)
    try:
        stats = capture(args.port, args.baud, args.duration, path, reset=not args.no_reset,
                        tx_fw=args.tx_fw)
    except TimeoutError as e:
        path.unlink(missing_ok=True)
        print(e, file=sys.stderr)
        return 1
    except serial.SerialException as e:
        print(f"シリアルの読み取りに失敗しました: {e}（記録: {path}）", file=sys.stderr)
        return 1

    print(stats.summary())
    for warning in stats.warnings():
        print(f"警告: {warning}", file=sys.stderr)
    write_meta_draft(path, args)
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
