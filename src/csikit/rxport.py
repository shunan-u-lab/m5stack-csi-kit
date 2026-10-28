"""受信機（firmware/csi-rx）のシリアルポートを開き、命令を送って応答を待つ.

- ``open_port``: リセットせずに開き、受信機の命令の行の途中に残ったゴミを捨てる
  （改行を 1 つ送ってその行を終わらせてから読み捨てる。scripts/sd_fetch.py と同じ手順）
- ``command``: 命令を 1 行送り、``# ack cmd=<命令>`` か ``# err cmd=<命令>`` が来るまで読む
  （その間の CSI の行などは ``on_line`` に渡すか捨てる）

失敗時の挙動: ポートが無いと FileNotFoundError、開けないと OSError。受信機が断ったら
CommandError（理由つき）、時間内に応答が無ければ TimeoutError。
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Protocol

from csikit.ports import port_missing

DEFAULT_PORT = "/dev/ttyACM0"  # R1（CH9102）
DEFAULT_BAUD = 921600  # firmware/csi-rx の SERIAL_BAUD
READ_TIMEOUT_SEC = 0.2  # 1 行を待つ時間 [sec]
LINE_FLUSH_SETTLE_SEC = 0.3  # 改行を送ってから捨てるまで待つ時間 [sec]
DEFAULT_REPLY_TIMEOUT_SEC = 5.0  # 応答を待つ時間 [sec]
_WHO = re.compile(r"^# who .*\bchannel=(\d+)\b.*\btx_mac=([0-9a-f]{12})\b")


class Port(Protocol):
    """シリアルポート（テストでは偽物に差し替える）."""

    def write(self, data: bytes) -> int | None: ...  # noqa: D102
    def readline(self) -> bytes: ...  # noqa: D102


class CommandError(RuntimeError):
    """受信機が命令を断った（``# err cmd=…``）."""


def open_port(port: str, baud: int = DEFAULT_BAUD) -> Port:
    """受信機をリセットせずにポートを開き、命令の行の途中を空にする.

    Args:
        port: シリアルポート。
        baud: ボーレート [bit/s]。

    Returns:
        開いたポート。

    Raises:
        FileNotFoundError: ポートが無い。
        OSError: 開けない。
    """
    import serial

    if port_missing(port):
        raise FileNotFoundError(f"ポートがありません: {port}（USB の共有を確認）")
    try:
        s = serial.Serial(port, baud, timeout=READ_TIMEOUT_SEC)
    except serial.SerialException as e:
        raise OSError(f"ポートを開けません: {e}") from e
    time.sleep(READ_TIMEOUT_SEC)
    s.write(b"\n")  # 開いたときのゴミの行を終わらせる
    time.sleep(LINE_FLUSH_SETTLE_SEC)
    s.reset_input_buffer()
    opened: Port = s
    return opened


def read_line(port: Port) -> str:
    """1 行を読む（末尾の改行を除く。読めない文字は置き換える）."""
    return port.readline().decode("ascii", errors="replace").rstrip("\r\n")


def command(port: Port, text: str, timeout_sec: float = DEFAULT_REPLY_TIMEOUT_SEC,
            on_line: Callable[[str], None] | None = None,
            reply: str | None = None) -> str:
    """命令を送り、応答の行を返す.

    Args:
        port: 開いたポート。
        text: 命令（改行なし。例: ``ALLCSI ON``）。
        timeout_sec: 応答を待つ時間 [sec]。
        on_line: 応答までに来たほかの行を渡す先（None なら捨てる）。
        reply: 待つ応答の行の始まり（既定は ``# ack cmd=<命令の最初の語>``）。

    Returns:
        応答の行。

    Raises:
        CommandError: ``# err cmd=<命令の最初の語>`` が来た。
        TimeoutError: 時間内に応答が来ない。
    """
    name = text.split()[0]
    want = reply if reply is not None else f"# ack cmd={name}"
    err = f"# err cmd={name}"
    port.write((text + "\n").encode("ascii"))
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        line = read_line(port)
        if not line:
            continue
        if line.startswith(want):
            return line
        if line.startswith(err):
            raise CommandError(f"{text}: {line}")
        if on_line is not None:
            on_line(line)
    raise TimeoutError(f"{text}: {timeout_sec:.0f} 秒以内に応答が無い"
                       "（受信機の版を WHO の fw= で確認）")


def who(port: Port) -> tuple[int, str]:
    """WHO で、今のチャンネルと主の送信機の MAC（12 桁）を返す.

    Raises:
        ValueError: WHO の応答の形が違う。
    """
    line = command(port, "WHO", reply="# who ")
    m = _WHO.match(line)
    if m is None:
        raise ValueError(f"WHO の応答の形が違う: {line!r}")
    return int(m.group(1)), m.group(2)
