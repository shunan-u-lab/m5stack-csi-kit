r"""受信機（R1）の SD カードのセッションを PC に取り込む（docs/decisions/0006）.

    uv run python scripts/sd_fetch.py time              # PC の時刻を受信機に教える（記録の前に）
    uv run python scripts/sd_fetch.py wifi --ssid <名前> # 起動時に時刻を取る Wi-Fi（夜間用）
    uv run python scripts/sd_fetch.py wifi --clear      # その設定を消す（借用機材を返す前など）
    uv run python scripts/sd_fetch.py list              # SD のファイルの一覧
    uv run python scripts/sd_fetch.py get S0003.csv     # USB 経由で取り込む（--delete あり）
    uv run python scripts/sd_fetch.py get --all         # まだ取り込んでいないものを全部
    uv run python scripts/sd_fetch.py rm S0003.csv      # SD から消す
    uv run python scripts/sd_fetch.py import-dir <dir>  # カード読み取り器で開いた CSI フォルダから
    uv run python scripts/sd_fetch.py events [--clear]  # 受信機の NVS の出来事を PC に保存

取り込んだファイルは ``<out-dir>/<日時>-<label>-<セッション>.csv``
（例 ``20261007-150000-sd-S0003.csv``）。日時はセッションの先頭の ``# capture start=``
（受信機に TIME で時刻を教えてあれば書かれる）。分からなければ取り込んだ時刻を使い、警告を出す
（メタデータの開始時刻は TODO のまま）。時刻を控えていれば ``--start 2026-10-07T23:10`` で与えられる
（1 ファイルずつ。先頭に ``# capture start=… source=manual`` の行を足す。それ以外は SD の
ファイルそのまま）。取り込んだ後、メタデータの下書き ``data/meta/<記録名>.toml`` を書く
（capture_csi.py と同じ）。続けて ``.csv.zst`` に圧縮し、展開した中身が元と一致することを
確かめてから ``.csv`` を消す（ADR 0004 の保管の形。1 時間分で約 30 秒）。圧縮に失敗したら
``.csv`` を残して警告する（取り込みは成功のまま）。``--no-compress`` で圧縮しない。

USB 経由の速さはシリアル（921600 bps）で約 90 KB/s が上限（1 時間 約 120 MB で約 25 分）。
大きいファイルはカード読み取り器（import-dir）の方が速い。転送は 4 KB ごとに CRC32 を確かめ、
合わなければその塊を送り直してもらう（受信機は 1 塊ごとに PC の返事を待つ）。

失敗時の挙動: ポートが無い・受信機が応答しない・CRC が合わないまま送り直しの回数を超えた・
同名のファイルがある → 何も書かずに（書きかけは消して）終了コード 1。
--delete は保存と確認が済んでから SD を消す。

受信機の NVS の出来事（SD の失敗・書き直し・立て直し。直近 16 件の輪）は、``events`` で
``data/processed/sd-diag/sdevents-<日時>.txt`` に保存する。``get`` のときも消さずに保存する。
``--clear`` は保存できた後で受信機から消す（次の晩の分で輪があふれないように）。
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import shutil
import sys
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from csikit import io, meta, ports, rawzst

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PORT = "/dev/ttyACM0"  # R1（CH9102）
DEFAULT_BAUD = 921600
DEFAULT_OUT_DIR = ROOT / "data" / "raw"
DEFAULT_META_DIR = ROOT / "data" / "meta"
DEFAULT_DIAG_DIR = ROOT / "data" / "processed" / "sd-diag"  # NVS の出来事の写し（ADR 0006）
DEFAULT_LABEL = "sd"
LABEL_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,30}")
SESSION_PATTERN = re.compile(r"S\d{4}\.csv")
FILE_LINE = re.compile(r"^# sd file name=(\S+) bytes=(\d+)")
GET_START = re.compile(r"^# sd get name=(\S+) size=(\d+) offset=(\d+)")
# 塊の見出しは行の途中にもありうる（前の塊のバイトが落ちて改行の前につながった場合）。search で探す
CHUNK_LINE = re.compile(r"#SDC (\d+) (\d+) ([0-9a-f]{8})$")
GET_ABORT = re.compile(r"# sd get abort name=(\S+) reason=(\S+)")
GET_DONE = re.compile(r"^# sd get done name=(\S+) bytes=(\d+)")
ERR_LINE = re.compile(r"^# err cmd=(\S+) reason=(\S+)")
CAPTURE_START = re.compile(r"^# capture start=(\S+)")
EVENTS_HEAD = re.compile(r"^# sdevents count=(\d+) total=(\d+)")
EVENT_LINE = re.compile(r"^# sdevent ")
EVENTS_CLEARED = re.compile(r"^# sdevents cleared")
EVENTS_FILE_PREFIX = "sdevents-"
# 消した応答。後ろの欄は 2026-10-10 の版から（古い受信機は name だけ）
RM_ACK = re.compile(r"^# ack cmd=SDRM name=(\S+)(?: bytes=(\d+) free_before_mb=(\d+) "
                    r"free_after_mb=(\d+) freed_mb=(-?\d+))?")
BYTES_PER_MIB = 1024 * 1024
# 増えた空きがファイルの大きさよりこれ以上少なければ警告する [MiB]
# （受信機は MiB の切り捨てで答える）
RM_FREE_TOLERANCE_MIB = 1
TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"
RESPONSE_TIMEOUT_SEC = 5.0  # 命令の応答を待つ時間
# SD から消す応答を待つ時間。2.2 GB のファイルは 5 秒では消し終わらなかった（2026-10-10）
RM_TIMEOUT_SEC = 120.0
CHUNK_TIMEOUT_SEC = 3.0     # 1 塊を待つ時間
MAX_RETRIES = 5             # CRC の不一致・途切れのときに送り直してもらう回数
READ_TIMEOUT_SEC = 0.2
# ポートを開いた直後に改行を送ってから、受信機の返事（ゴミの行への # err）を捨てるまで待つ時間 [sec]
# （受信機は 10 ms ごとに命令を読む）
LINE_FLUSH_SETTLE_SEC = 0.2
ABORT_SETTLE_SEC = 6.0      # 頼み直しの前に、受信機が返事の時間切れ（5 秒）でやめるのを待つ
HEADER_SCAN_LINES = 40      # 先頭のこれだけの行から # capture start を探す
TMP_SUFFIX = ".part"
WIFI_SSID_ENV = "CSI_WIFI_SSID"  # 省略時の Wi-Fi の名前
WIFI_PASS_ENV = "CSI_WIFI_PASS"  # 省略時のパスワード（無ければ入力を求める。画面に出さない）
SSID_MAX_BYTES = 32   # IEEE 802.11 の上限 [byte]
PASS_MIN_BYTES = 8    # WPA2 の下限 [byte]（0 = 暗号なしの Wi-Fi）
PASS_MAX_BYTES = 63   # WPA2 の上限 [byte]
NTP_BOOT_TIMEOUT_SEC = 45.0  # 再起動から # ntp の行までを待つ上限（接続 15 秒・NTP 10 秒＋起動）
NTP_LINE = re.compile(r"^# ntp ok=(\d)(.*)$")


class Port(Protocol):
    """シリアルポート（テストでは偽物に差し替える）."""

    def write(self, data: bytes) -> int | None: ...  # noqa: D102
    def readline(self) -> bytes: ...  # noqa: D102
    def read(self, size: int) -> bytes: ...  # noqa: D102


class FetchError(RuntimeError):
    """受信機との受け渡しの失敗."""


@dataclass(frozen=True)
class SdFile:
    """SD のファイル 1 つ.

    Attributes:
        name: ファイル名（S0001.csv）。
        size: 大きさ [byte]。
    """

    name: str
    size: int


def send_command(port: Port, command: str) -> None:
    """命令を 1 行送る.

    Args:
        port: シリアルポート。
        command: 命令（改行なし）。
    """
    port.write((command + "\n").encode("ascii"))


def read_until(port: Port, pattern: re.Pattern[str], timeout_sec: float,
               clock: Callable[[], float] = time.monotonic) -> tuple[re.Match[str], list[str]]:
    """``pattern`` に合う行が来るまで読む（CSI 行などは飛ばす。# err で止める）.

    Args:
        port: シリアルポート。
        pattern: 待つ行。
        timeout_sec: 待つ上限 [sec]。
        clock: 時計（テストで差し替える）。

    Returns:
        (合った行, それまでに読んだ # 行)。

    Raises:
        FetchError: 時間切れ・# err が来た場合。
    """
    notes: list[str] = []
    t0 = clock()
    while clock() - t0 < timeout_sec:
        raw = port.readline()
        if not raw:
            continue
        line = raw.decode("ascii", errors="replace").rstrip("\r\n")
        if m := pattern.search(line):
            return m, notes
        if ERR_LINE.search(line):
            raise FetchError(f"受信機が断りました: {line}")
        if GET_ABORT.search(line):
            raise FetchError(f"受信機が転送をやめました: {line}")
        if line.startswith("#"):
            notes.append(line)
    raise FetchError(f"応答がありません（{timeout_sec:.0f} 秒）: {pattern.pattern}")


def read_events(port: Port, clock: Callable[[], float] = time.monotonic) -> list[str]:
    """受信機の NVS に残った SD の出来事（``# sdevent`` 行）を読む（消さない）.

    Args:
        port: シリアルポート。
        clock: 時計（テストで差し替える）。

    Returns:
        ``# sdevents`` の見出しと ``# sdevent`` 行（古い順）。

    Raises:
        FetchError: 時間切れ・行が足りない場合。
    """
    send_command(port, "SDEVENTS")
    m, _ = read_until(port, EVENTS_HEAD, RESPONSE_TIMEOUT_SEC, clock)
    count = int(m.group(1))
    lines = [m.group(0)]
    for _ in range(count):
        ev, _ = read_until(port, EVENT_LINE, RESPONSE_TIMEOUT_SEC, clock)
        lines.append(ev.string)
    return lines


def save_events(lines: list[str], diag_dir: Path, now: datetime) -> Path:
    """出来事の行を ``<diag_dir>/sdevents-<時刻>.txt`` に書く.

    受信機の出来事は直近 16 件の輪で、一晩で古いものが消える（2026-10-10）。読んだら PC に残す。

    Args:
        lines: ``read_events`` の結果。
        diag_dir: 置き場所。
        now: 読んだ時刻（ファイル名に使う）。

    Returns:
        書いたファイル。

    Raises:
        ValueError: 見出しが無い場合。
    """
    if not lines or not EVENTS_HEAD.search(lines[0]):
        raise ValueError("出来事の見出し（# sdevents）がありません")
    diag_dir.mkdir(parents=True, exist_ok=True)
    path = diag_dir / f"{EVENTS_FILE_PREFIX}{now.strftime(TIMESTAMP_FORMAT)}.txt"
    head = f"# read_at={now.astimezone().isoformat(timespec='seconds')}\n"
    path.write_text(head + "\n".join(lines) + "\n", encoding="utf-8")
    return path


def fetch_events(port: Port, diag_dir: Path, now: datetime, clear: bool) -> Path:
    """出来事を読んで保存し、``clear`` なら保存できた後で受信機から消す.

    Args:
        port: シリアルポート。
        diag_dir: 置き場所。
        now: 読んだ時刻。
        clear: 保存した後に受信機の出来事を消す。

    Returns:
        書いたファイル。
    """
    lines = read_events(port)
    path = save_events(lines, diag_dir, now)
    if clear:
        send_command(port, "SDEVENTS CLEAR")
        read_until(port, EVENTS_CLEARED, RESPONSE_TIMEOUT_SEC)
    return path


def remove_file(port: Port, name: str) -> str | None:
    """SD のファイルを消し、空きが消した分だけ増えたかを確かめる.

    2026-10-10 に、24 本を消しても受信機の空きが増えず、再起動しても戻らなかった
    （カードの使用状況の表がずれていた）。受信機の応答の空き（消す前・後）とファイルの大きさを比べ、その場で気づけるようにする。

    Args:
        port: シリアルポート。
        name: ファイル名（S0003.csv）。

    Returns:
        空きの増え方がおかしければ警告の文。問題なし・古い受信機で分からなければ None。

    Raises:
        FetchError: 断られた・応答が無い場合。
    """
    send_command(port, f"SDRM {name}")
    m, _ = read_until(port, RM_ACK, RM_TIMEOUT_SEC)
    return check_freed(m)


def check_freed(m: re.Match[str]) -> str | None:
    """``RM_ACK`` に合った応答から、空きが消した分だけ増えたかを判定する.

    Args:
        m: ``RM_ACK`` の結果。

    Returns:
        足りなければ警告の文。足りている・欄が無い（古い受信機）なら None。
    """
    if m.group(2) is None:
        return None
    expected_mib = int(m.group(2)) // BYTES_PER_MIB
    freed_mib = int(m.group(5))
    if freed_mib + RM_FREE_TOLERANCE_MIB >= expected_mib:
        return None
    return (f"{m.group(1)}: 空きが {freed_mib} MiB しか増えていません"
            f"（ファイルは {expected_mib} MiB。空き {m.group(3)} → {m.group(4)} MiB）。"
            "カードの使用状況の表がずれている可能性があります")


def list_files(port: Port) -> list[SdFile]:
    """SD のファイルの一覧を取る.

    Args:
        port: シリアルポート。

    Returns:
        ファイルの一覧（名前順）。

    Raises:
        FetchError: 応答がない・断られた場合。
    """
    send_command(port, "SDLS")
    _, notes = read_until(port, re.compile(r"^# sd ls done"), RESPONSE_TIMEOUT_SEC)
    files = [SdFile(m.group(1), int(m.group(2))) for n in notes if (m := FILE_LINE.match(n))]
    return sorted(files, key=lambda f: f.name)


def _read_exact(port: Port, size: int, timeout_sec: float,
                clock: Callable[[], float] = time.monotonic) -> bytes:
    """ちょうど ``size`` バイト読む（時間切れなら FetchError）."""
    buf = bytearray()
    t0 = clock()
    while len(buf) < size:
        if clock() - t0 > timeout_sec:
            raise FetchError(f"塊の途中で途切れました（{len(buf)}/{size} バイト）")
        buf += port.read(size - len(buf))
    return bytes(buf)


def fetch(port: Port, name: str, dest: Path, progress: Callable[[int, int], None] | None = None,
          clock: Callable[[], float] = time.monotonic) -> int:
    """SD のファイルを ``dest`` に取り込む（1 塊ずつ CRC32 を確かめて返事をする）.

    受信機は 4 KB の塊を 1 つ送るごとに返事を待つ。CRC が合えば ``K``（次へ）、合わない・途切れたら
    ``R``（同じ塊をもう一度）を返す。受信機がやめた（返事の時間切れ等）ときは、取り込んだ所から
    頼み直す（``MAX_RETRIES`` 回まで）。

    Args:
        port: シリアルポート。
        name: ファイル名（S0001.csv）。
        dest: 書き出し先。
        progress: (取り込んだ量, 全体) を受ける関数。
        clock: 時計（テストで差し替える）。

    Returns:
        取り込んだ大きさ [byte]。

    Raises:
        FetchError: 断られた・送り直しの回数を超えた・大きさが合わない場合。
    """
    if not SESSION_PATTERN.fullmatch(name):
        raise FetchError(f"セッションのファイル名ではありません: {name}")
    done = 0
    size = -1
    restarts = 0
    with dest.open("wb") as out:
        while size < 0 or done < size:
            send_command(port, f"SDGET {name} {done}")
            try:
                m, _ = read_until(port, GET_START, RESPONSE_TIMEOUT_SEC, clock)
                size = int(m.group(2))
                if int(m.group(3)) != done:
                    raise FetchError(f"位置が合いません: {m.group(0)}")
                bad = 0
                while done < size:
                    m, _ = read_until(port, CHUNK_LINE, CHUNK_TIMEOUT_SEC, clock)
                    off, n, crc = int(m.group(1)), int(m.group(2)), int(m.group(3), 16)
                    try:
                        data = _read_exact(port, n, CHUNK_TIMEOUT_SEC, clock)
                        ok = off == done and zlib.crc32(data) == crc
                    except FetchError:
                        ok = False
                    if not ok:
                        bad += 1
                        if bad > MAX_RETRIES:
                            send_command(port, "Q")
                            raise FetchError(
                                f"同じ塊が {MAX_RETRIES} 回続けて壊れました（位置 {done}）")
                        send_command(port, "R")
                        continue
                    out.write(data)
                    done += n
                    bad = 0
                    send_command(port, "K")
                    if progress is not None:
                        progress(done, size)
                read_until(port, GET_DONE, RESPONSE_TIMEOUT_SEC, clock)
            except FetchError as e:
                if "断りました" in str(e) or "続けて壊れました" in str(e):
                    raise
                restarts += 1
                if restarts > MAX_RETRIES:
                    raise FetchError(f"頼み直しの回数（{MAX_RETRIES}）を超えました: {e}") from e
                print(f"\n頼み直し {restarts}/{MAX_RETRIES}（{done} バイトから）: {e}",
                      file=sys.stderr)
                time.sleep(ABORT_SETTLE_SEC)  # 受信機が返事の時間切れでやめるのを待つ
    if done != size:
        raise FetchError(f"大きさが合いません: {done}/{size}")
    return done


def wifi_command(ssid: str, password: str, tz_sec: int) -> str:
    """受信機に送る WIFISET の命令（名前とパスワードは 16 進にして、空白や記号を気にしない）.

    Args:
        ssid: Wi-Fi の名前。
        password: パスワード（空 = 暗号なし）。
        tz_sec: UTC からの差 [sec]。

    Returns:
        ``WIFISET <名前の16進> <パスワードの16進 か -> <差>``。

    Raises:
        ValueError: 長さが範囲の外の場合。
    """
    ssid_bytes = ssid.encode("utf-8")
    pass_bytes = password.encode("utf-8")
    if not 1 <= len(ssid_bytes) <= SSID_MAX_BYTES:
        raise ValueError(f"Wi-Fi の名前は 1〜{SSID_MAX_BYTES} バイト: {len(ssid_bytes)}")
    if pass_bytes and not PASS_MIN_BYTES <= len(pass_bytes) <= PASS_MAX_BYTES:
        raise ValueError(f"パスワードは {PASS_MIN_BYTES}〜{PASS_MAX_BYTES} バイト"
                         f"（暗号なしなら空）: {len(pass_bytes)}")
    return f"WIFISET {ssid_bytes.hex()} {pass_bytes.hex() or '-'} {tz_sec}"


def parse_start(text: str) -> datetime:
    """``--start`` の時刻を読む（タイムゾーンが無ければ PC の地域の時刻とみなす）.

    Args:
        text: ``2026-10-07T23:10`` や ``2026-10-07 23:10:30+09:00``。

    Returns:
        タイムゾーンつきの時刻。

    Raises:
        ValueError: 読めない場合。
    """
    t = datetime.fromisoformat(text)
    return t if t.tzinfo else t.astimezone()


def add_manual_start(path: Path, start: datetime) -> None:
    """取り込んだファイルの先頭に ``# capture start=… source=manual`` を足す（書き直す）.

    Args:
        path: 取り込んだ（書きかけの）ファイル。
        start: 利用者が控えた開始時刻（タイムゾーンつき）。
    """
    tmp = path.with_name(path.name + ".start")
    line = f"# capture start={start.isoformat(timespec='seconds')} source=manual\n"
    try:
        with tmp.open("wb") as out, path.open("rb") as src:
            out.write(line.encode("ascii"))
            shutil.copyfileobj(src, out)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def start_time_from_header(path: Path) -> datetime | None:
    """セッションの先頭の ``# capture start=`` の時刻（無ければ None）.

    Args:
        path: 取り込んだファイル。

    Returns:
        時刻（タイムゾーンつき）。
    """
    with path.open(encoding="ascii", errors="replace") as f:
        for i, line in enumerate(f):
            if i >= HEADER_SCAN_LINES:
                break
            if m := CAPTURE_START.match(line):
                try:
                    return datetime.fromisoformat(m.group(1))
                except ValueError:
                    return None
    return None


def output_path(out_dir: Path, label: str, session: str, start: datetime | None,
                now: datetime) -> Path:
    """取り込んだファイルの置き場所を決める（上書きしない）.

    Args:
        out_dir: 置き場。
        label: ラベル（英数字・_・-）。
        session: セッションのファイル名（S0003.csv）。
        start: セッションの開始時刻（分からなければ None）。
        now: 取り込んだ時刻（start が無いとき使う）。

    Returns:
        ``<out_dir>/<日時>-<label>-<S0003>.csv``。

    Raises:
        ValueError: ラベルが不正な場合。
        FileExistsError: 同名がある場合。
    """
    if not LABEL_PATTERN.fullmatch(label):
        raise ValueError(f"ラベルは英数字・_・- で 1〜30 文字: {label!r}")
    stamp = (start.astimezone() if start else now).strftime(TIMESTAMP_FORMAT)
    path = out_dir / f"{stamp}-{label}-{session.removesuffix('.csv')}.csv"
    for existing in (path, path.with_name(path.name + ".zst")):
        if existing.exists():
            raise FileExistsError(f"既にあります（上書きしません）: {existing}")
    return path


def already_imported(out_dir: Path, label: str, session: str,
                     start: datetime | None = None) -> list[Path]:
    """同じラベル・セッション番号（開始時刻が分かればそれも同じ）の取り込み済みのファイル.

    カードを空にするとセッションの番号は S0001 から振り直されるので、番号だけでは別の記録を
    取り込み済みと誤る（2026-10-08: 前日の S0001 があるため、当日の S0001 が飛ばされた）。
    開始時刻（``# capture start=``）が分かるときは、名前の日時も一致するものだけを数える。

    Args:
        out_dir: 置き場。
        label: ラベル。
        session: セッションのファイル名。
        start: セッションの開始時刻（元のファイルの先頭から読めたとき。None なら番号だけで判定）。

    Returns:
        見つかったファイル。
    """
    stem = session.removesuffix(".csv")
    prefix = start.astimezone().strftime(TIMESTAMP_FORMAT) if start is not None else "*"
    return sorted(p for suffix in io.RAW_SUFFIXES
                  for p in out_dir.glob(f"{prefix}-{label}-{stem}{suffix}"))


def finish_import(tmp: Path, out_dir: Path, label: str, session: str, args: argparse.Namespace,
                  now: datetime) -> Path:
    """書きかけのファイルに名前を付け、メタデータの下書きを書き、圧縮する.

    Args:
        tmp: 取り込んだ（書きかけの）ファイル。
        out_dir: 置き場。
        label: ラベル。
        session: セッションのファイル名。
        args: --experiment・--meta-base・--meta-dir・--no-meta・--no-compress・--level を含む。
        now: 取り込んだ時刻。

    Returns:
        置いたファイル（圧縮できれば ``.csv.zst``、できなければ ``.csv``）。
    """
    start = start_time_from_header(tmp)
    manual: datetime | None = getattr(args, "start", None)
    if start is not None and manual is not None:
        print(f"警告: {session} には受信機の開始時刻 {start.isoformat()} があるので、"
              "--start は使いません", file=sys.stderr)
    elif manual is not None:
        add_manual_start(tmp, manual)
        start = manual
    if start is None:
        print(f"警告: {session} に開始時刻がありません（受信機が時刻を知らなかった）。"
              "取り込んだ時刻で名前を付けます。控えていれば --start で与えられます",
              file=sys.stderr)
    path = output_path(out_dir, label, session, start, now)
    tmp.replace(path)
    if not args.no_meta:
        try:
            draft = meta.write_draft(path, args.meta_dir, args.experiment, args.meta_base)
            print(f"メタデータの下書き: {draft}", file=sys.stderr)
        except (OSError, ValueError) as e:
            print(f"警告: メタデータの下書きを書けませんでした: {e}", file=sys.stderr)
    if args.no_compress:
        return path
    return compress_imported(path, args.level)


def compress_imported(path: Path, level: int) -> Path:
    """取り込んだ ``.csv`` を ``.csv.zst`` にし、中身の一致を確かめてから ``.csv`` を消す.

    失敗しても取り込みは済んでいるので、``.csv`` を残して警告するだけにする
    （後から ``raw_store.py compress --delete-original`` で圧縮できる）。

    Args:
        path: 取り込んだ ``.csv``。
        level: zstd の圧縮の強さ。

    Returns:
        ``.csv.zst``（失敗したら ``path`` のまま）。
    """
    try:
        zst: Path = rawzst.compress_verified(path, level)
    except (OSError, ValueError) as e:
        print(f"警告: 圧縮できませんでした（{path.name} は .csv のまま残します）: {e}",
              file=sys.stderr)
        return path
    path.unlink()
    return zst


def open_port(port: str, baud: int) -> Port:
    """受信機をリセットせずにポートを開き、受信機の命令の行の途中を空にする.

    ポートを開く・閉じると、受信機の命令の行の途中にゴミの文字が残ることがある（2026-10-11:
    最初の命令が ``# err cmd=^Z:SDEVENTS reason=unknown`` と断られた）。改行を 1 つ送ってその行を
    終わらせ（受信機は空の行を無視し、ゴミの行には ``# err`` を返す）、返事を待ってから捨てる。

    Args:
        port: シリアルポート。
        baud: ボーレート [bit/s]。

    Returns:
        開いたポート。

    Raises:
        FetchError: ポートが無い・開けない場合。
    """
    import serial

    if ports.port_missing(port):
        raise FetchError(f"ポートがありません: {port}（USB の共有を確認）")
    try:
        s = serial.Serial(port, baud, timeout=READ_TIMEOUT_SEC)
    except serial.SerialException as e:
        raise FetchError(f"ポートを開けません: {e}") from e
    time.sleep(READ_TIMEOUT_SEC)
    s.write(b"\n")
    time.sleep(LINE_FLUSH_SETTLE_SEC)
    s.reset_input_buffer()
    opened: Port = s
    return opened


def main(argv: list[str] | None = None) -> int:
    """コマンドラインから取り込む.

    Args:
        argv: 引数（省略時は ``sys.argv``）。

    Returns:
        終了コード（0: 成功 / 1: 失敗）。
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", default=DEFAULT_PORT)
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--label", default=DEFAULT_LABEL, help="ファイル名のラベル（既定 sd）")
    parser.add_argument("--experiment", default=meta.TODO, help="メタデータの実験の番号")
    parser.add_argument("--meta-base", type=Path, help="配置などを写すメタデータ")
    parser.add_argument("--meta-dir", type=Path, default=DEFAULT_META_DIR)
    parser.add_argument("--no-meta", action="store_true", help="メタデータの下書きを書かない")
    parser.add_argument("--no-compress", action="store_true",
                        help="取り込んだ後に .csv.zst に圧縮しない（既定は圧縮して .csv を消す）")
    parser.add_argument("--level", type=int, default=rawzst.DEFAULT_LEVEL,
                        help=f"zstd の圧縮の強さ（1〜{rawzst.MAX_LEVEL}）")
    parser.add_argument("--start", type=parse_start,
                        help="開始時刻（受信機が時刻を知らなかったセッション用。1 ファイルずつ）")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("time", help="PC の時刻を受信機に教える")
    sub.add_parser("list", help="SD のファイルの一覧")
    pw = sub.add_parser("wifi", help="起動時に NTP で時刻を取る Wi-Fi を設定する（受信機の NVS）")
    pw.add_argument("--ssid", default=os.environ.get(WIFI_SSID_ENV),
                    help=f"Wi-Fi の名前（省略時は環境変数 {WIFI_SSID_ENV}）")
    pw.add_argument("--clear", action="store_true", help="設定を消す")
    pw.add_argument("--no-reboot", action="store_true",
                    help="設定した後に受信機を再起動して時刻が取れるか確かめない")
    pg = sub.add_parser("get", help="USB 経由で取り込む")
    pg.add_argument("names", nargs="*", help="ファイル名（S0003.csv）")
    pg.add_argument("--all", action="store_true", help="まだ取り込んでいないものを全部")
    pg.add_argument("--delete", action="store_true", help="確かめた後に SD から消す")
    pr = sub.add_parser("rm", help="SD から消す")
    pr.add_argument("names", nargs="+")
    pe = sub.add_parser("events", help="受信機の NVS の出来事（SD の失敗・立て直し）を保存する")
    pe.add_argument("--clear", action="store_true", help="保存した後に受信機から消す")
    pe.add_argument("--diag-dir", type=Path, default=DEFAULT_DIAG_DIR)
    pi = sub.add_parser("import-dir", help="カード読み取り器で開いたフォルダから取り込む")
    pi.add_argument("dir", type=Path, help="SD カードの CSI フォルダ")
    args = parser.parse_args(argv)

    if not LABEL_PATTERN.fullmatch(args.label):
        print(f"--label は英数字・_・- で 1〜30 文字: {args.label!r}", file=sys.stderr)
        return 1
    if not 1 <= args.level <= rawzst.MAX_LEVEL:
        print(f"--level は 1〜{rawzst.MAX_LEVEL}: {args.level}", file=sys.stderr)
        return 1
    now = datetime.now()
    try:
        if args.command == "import-dir":
            return import_dir(args, now)
        port = open_port(args.port, args.baud)
        if args.command == "time":
            tz = int(now.astimezone().utcoffset().total_seconds())  # type: ignore[union-attr]
            send_command(port, f"TIME {int(time.time())} {tz}")
            m, _ = read_until(port, re.compile(r"^# ack cmd=TIME now=(\S+)"), RESPONSE_TIMEOUT_SEC)
            print(f"受信機の時刻: {m.group(1)}")
            return 0
        if args.command == "wifi":
            return set_wifi(port, args, now)
        if args.command == "list":
            for f in list_files(port):
                print(f"{f.name}\t{f.size} バイト\t{f.size / 1e6:.1f} MB")
            return 0
        if args.command == "rm":
            for name in args.names:
                warning = remove_file(port, name)
                print(f"消しました: {name}")
                if warning:
                    print(f"警告: {warning}", file=sys.stderr)
            return 0
        if args.command == "events":
            path = fetch_events(port, args.diag_dir, now, args.clear)
            cleared = "（受信機からは消しました）" if args.clear else ""
            print(f"出来事を保存しました: {path}{cleared}")
            return 0
        code = get_files(port, args, now)
        # USB で取り込んだついでに、出来事も残す（消さない。失敗しても取り込みの結果は変えない）
        try:
            print(f"出来事を保存しました: {fetch_events(port, DEFAULT_DIAG_DIR, now, clear=False)}")
        except (FetchError, OSError, ValueError) as e:
            print(f"出来事を読めませんでした: {e}", file=sys.stderr)
        return code
    except (FetchError, OSError, ValueError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1


def set_wifi(port: Port, args: argparse.Namespace, now: datetime) -> int:
    """``wifi`` を処理する（設定して、再起動して NTP の結果を見る）.

    パスワードは環境変数か入力から受け取り、画面・記録には出さない。

    Args:
        port: シリアルポート。
        args: 引数。
        now: 今の時刻（UTC からの差を決める）。

    Returns:
        終了コード。
    """
    if args.clear:
        send_command(port, "WIFICLEAR")
        read_until(port, re.compile(r"^# ack cmd=WIFICLEAR"), RESPONSE_TIMEOUT_SEC)
        print("受信機の Wi-Fi の設定を消しました（次の起動から NTP を使いません）")
        return 0
    if not args.ssid:
        print(f"--ssid か環境変数 {WIFI_SSID_ENV} で Wi-Fi の名前を与えてください", file=sys.stderr)
        return 1
    password = os.environ.get(WIFI_PASS_ENV)
    if password is None:
        password = getpass.getpass(f"{args.ssid} のパスワード（暗号なしなら空）: ")
    tz = int(now.astimezone().utcoffset().total_seconds())  # type: ignore[union-attr]
    send_command(port, wifi_command(args.ssid, password, tz))
    read_until(port, re.compile(r"^# ack cmd=WIFISET"), RESPONSE_TIMEOUT_SEC)
    print("受信機に Wi-Fi を設定しました")
    if args.no_reboot:
        return 0
    if recording(port):
        print("SD に記録中です。再起動すると新しいセッションで再開します", file=sys.stderr)
    send_command(port, "REBOOT")
    m, _ = read_until(port, NTP_LINE, NTP_BOOT_TIMEOUT_SEC)
    if m.group(1) == "1":
        print(f"再起動して時刻を取れました:{m.group(2)}")
        return 0
    print(f"再起動しましたが時刻を取れませんでした:{m.group(2)}", file=sys.stderr)
    return 1


def recording(port: Port) -> bool:
    """受信機が SD に記録中か（SDINFO で聞く）.

    Args:
        port: シリアルポート。

    Returns:
        記録中なら True。
    """
    send_command(port, "SDINFO")
    m, _ = read_until(port, re.compile(r"^# sd info mounted=\d recording=(\d)"),
                      RESPONSE_TIMEOUT_SEC)
    return m.group(1) == "1"


def get_files(port: Port, args: argparse.Namespace, now: datetime) -> int:
    """``get`` を処理する.

    Args:
        port: シリアルポート。
        args: 引数。
        now: 取り込んだ時刻。

    Returns:
        終了コード。
    """
    files = {f.name: f for f in list_files(port)}
    names = list(args.names)
    if args.all:
        names += [n for n in files if not already_imported(args.out_dir, args.label, n)]
    if not names:
        print("取り込むファイルがありません", file=sys.stderr)
        return 0
    if args.start is not None and len(names) != 1:
        print("--start は 1 ファイルずつ使ってください", file=sys.stderr)
        return 1
    failed = 0
    for name in names:
        if name not in files:
            print(f"失敗: SD にありません: {name}", file=sys.stderr)
            failed += 1
            continue
        args.out_dir.mkdir(parents=True, exist_ok=True)
        tmp = args.out_dir / f".{args.label}-{name}{TMP_SUFFIX}"
        t0 = time.monotonic()
        try:
            n = fetch(port, name, tmp, progress=_progress)
            path = finish_import(tmp, args.out_dir, args.label, name, args, now)
        except (FetchError, OSError, ValueError) as e:
            tmp.unlink(missing_ok=True)
            print(f"\n失敗: {name}: {e}", file=sys.stderr)
            failed += 1
            continue
        dt = time.monotonic() - t0
        print(f"\n{name} → {path}（{n} バイト・{dt:.0f} 秒・{n / max(dt, 1e-6) / 1e3:.0f} KB/s）")
        if args.delete:
            warning = remove_file(port, name)
            print(f"SD から消しました: {name}")
            if warning:
                print(f"警告: {warning}", file=sys.stderr)
    return 1 if failed else 0


def _progress(done: int, size: int) -> None:
    """取り込みの進み具合を 1 行で上書き表示する."""
    print(f"\r{done / 1e6:.1f}/{size / 1e6:.1f} MB", end="", file=sys.stderr, flush=True)


def import_dir(args: argparse.Namespace, now: datetime) -> int:
    """カード読み取り器で開いたフォルダの S????.csv を取り込む（既に取り込んだものは飛ばす）.

    Args:
        args: 引数（dir を含む）。
        now: 取り込んだ時刻。

    Returns:
        終了コード。
    """
    if not args.dir.is_dir():
        print(f"フォルダがありません: {args.dir}", file=sys.stderr)
        return 1
    sources = sorted(p for p in args.dir.iterdir() if SESSION_PATTERN.fullmatch(p.name))
    if not sources:
        print(f"セッションのファイル（S0001.csv など）がありません: {args.dir}", file=sys.stderr)
        return 1
    # 元のファイルの先頭から開始時刻を読み、日時まで同じものだけを取り込み済みとする
    pending = [s for s in sources
               if not already_imported(args.out_dir, args.label, s.name, start_time_from_header(s))]
    for src in sources:
        if src not in pending:
            print(f"飛ばす（取り込み済み）: {src.name}")
    if args.start is not None and len(pending) != 1:
        print(f"--start は 1 ファイルずつ使ってください（取り込むのは {len(pending)} 個）",
              file=sys.stderr)
        return 1
    args.out_dir.mkdir(parents=True, exist_ok=True)
    failed = 0
    for src in pending:
        tmp = args.out_dir / f".{args.label}-{src.name}{TMP_SUFFIX}"
        try:
            shutil.copyfile(src, tmp)
            path = finish_import(tmp, args.out_dir, args.label, src.name, args, now)
        except (OSError, ValueError) as e:
            tmp.unlink(missing_ok=True)
            print(f"失敗: {src.name}: {e}", file=sys.stderr)
            failed += 1
            continue
        print(f"{src.name} → {path}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
