"""M5Stack Basic のシリアルポートを探し、リセットせずに開き、WHO で送信機か受信機かを見分ける.

- ``list_ports``: USB のシリアルポートを一覧にする（M5Stack Basic の USB 変換チップに印を付ける。
  新しい版は CH9102、古い版は CP2104）
- ``open_no_reset``: 開いてから RTS → DTR の順に下げる。M5Stack Basic は DTR/RTS が本体の
  リセットにつながっていて、「DTR だけ下がり RTS は上がっている」瞬間があると再起動する
  （受信機は時刻の取得を待つ間、最大で約 30 秒黙る）
- ``identify``: ``WHO`` を送り、応答から役割・MAC・組の状態を読む。送信機は 115200 bps、
  受信機は 921600 bps なので、両方を順に試す

失敗時の挙動: 開けないポートは OSError。``identify`` は、どの速さでも ``WHO`` に答えなければ
None を返す（ファーム未書き込み・別の機器・別のアプリがポートを使用中のどれか）。
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

# M5Stack Basic の USB 変換チップ（ベンダー ID, プロダクト ID）→ チップ名。ドライバの案内に使う
KNOWN_CHIPS = {
    (0x1A86, 0x55D4): "CH9102",  # WCH。新しい版の M5Stack Basic
    (0x10C4, 0xEA60): "CP2104",  # Silicon Labs。古い版の M5Stack Basic
}
TX_BAUD = 115200  # firmware/csi-tx の SERIAL_BAUD [bit/s]
RX_BAUD = 921600  # firmware/csi-rx の SERIAL_BAUD [bit/s]
# 受信機を先に試す（出力が多く、違う速さで開いても化けるだけで害はない）
IDENTIFY_BAUDS = (RX_BAUD, TX_BAUD)
READ_TIMEOUT_SEC = 0.2  # 1 行を待つ時間 [sec]
SETTLE_SEC = 0.2  # 開いてから、途中の行を捨てるまで待つ時間 [sec]
WHO_TIMEOUT_SEC = 1.5  # WHO の応答を待つ時間 [sec]
MAC_HEX_LEN = 12  # 区切りなしの MAC の長さ [文字]

_WHO = re.compile(r"^(?:\[tx\]|#) who (?P<body>.*)$")
_KV = re.compile(r"(\w+)=(\S+)")


class SerialPort(Protocol):
    """開いたシリアルポート（テストでは偽物に差し替える）."""

    def write(self, data: bytes) -> int | None: ...  # noqa: D102
    def readline(self) -> bytes: ...  # noqa: D102
    def reset_input_buffer(self) -> None: ...  # noqa: D102
    def close(self) -> None: ...  # noqa: D102


Opener = Callable[[str, int], SerialPort]


@dataclass(frozen=True)
class PortInfo:
    """USB のシリアルポート 1 つ."""

    device: str  # 例: /dev/ttyACM0・COM3・/dev/cu.usbserial-…
    description: str
    vid: int | None
    pid: int | None

    @property
    def chip(self) -> str | None:
        """M5Stack Basic の USB 変換チップに見えればその名前（違えば None）."""
        if self.vid is None or self.pid is None:
            return None
        return KNOWN_CHIPS.get((self.vid, self.pid))


@dataclass(frozen=True)
class Board:
    """``WHO`` に答えた M5Stack Basic."""

    port: str
    baud: int  # [bit/s]
    role: str  # "tx"（送信機）か "rx"（受信機）
    mac: str  # 12 桁の 16 進（小文字・区切りなし）
    peer_mac: str  # 組んだ相手の MAC（未設定なら 000000000000）
    paired: bool | None  # None = 組の命令が無い古いファーム
    fw: str  # ファームウェアの版（例: 1027-1a2b3c4）
    who: str  # 応答の行そのもの（点検の表示用）


def list_ports(lister: Callable[[], Sequence[Any]] | None = None) -> list[PortInfo]:
    """USB のシリアルポートを一覧にする（M5Stack Basic に見えるものを先に並べる）.

    Args:
        lister: ``serial.tools.list_ports.comports`` の代わり（テスト用）。

    Returns:
        ポートの一覧。
    """
    if lister is None:
        from serial.tools import list_ports as lp

        lister = lp.comports
    infos = [PortInfo(device=p.device, description=p.description or "", vid=p.vid, pid=p.pid)
             for p in lister()]
    usb = [p for p in infos if p.vid is not None]  # 内蔵のシリアル（ttyS0 など）は除く
    return sorted(usb, key=lambda p: (p.chip is None, p.device))


def port_missing(port: str) -> bool:
    """ポートが無いと分かるか（``/dev/`` のポートだけファイルの有無で確かめる）.

    Windows の ``COM3`` などはファイルではないので、ここでは無いと決めない（開いて確かめる）。
    """
    return port.startswith("/dev/") and not Path(port).exists()


def open_no_reset(port: str, baud: int) -> SerialPort:
    """M5Stack Basic を再起動させずにポートを開き、DTR/RTS を下げる.

    開く前に ``dtr = False`` を決めておく形は使わない。Linux では開いた瞬間にカーネルが
    DTR/RTS を両方上げ、pyserial が DTR → RTS の順に 1 本ずつ下げるので、間に「DTR だけ
    下がる」瞬間ができて再起動する（2026-10-28 に実機の CH9102 で 3 回中 3 回）。
    両方上がったままなら再起動しないので、普通に開いてから RTS → DTR の順に下げる
    （同じ日に 3 回中 0 回。閉じても再起動しないことを送信機の連番で確かめた）。

    Args:
        port: ポート名。
        baud: ボーレート [bit/s]。

    Returns:
        開いたポート。

    Raises:
        OSError: 開けない（無い・権限が無い・別のアプリが使用中）。
    """
    import serial

    try:
        s = serial.Serial(port, baud, timeout=READ_TIMEOUT_SEC)
    except serial.SerialException as e:
        raise OSError(f"{port} を開けません: {e}") from e
    s.rts = False  # 先に RTS。DTR を先に下げると再起動する
    s.dtr = False
    opened: SerialPort = s
    return opened


def read_line(port: SerialPort) -> str:
    """1 行を読む（末尾の改行を除く。読めない文字は置き換える）."""
    return port.readline().decode("ascii", errors="replace").rstrip("\r\n")


def flush_partial_line(port: SerialPort, sleep: Callable[[float], None] = time.sleep) -> None:
    """改行を 1 つ送って、相手の命令の受け口に残った途中の行を終わらせ、届いていた行を捨てる.

    空の行はどちらのファームも黙って捨てる。
    """
    port.write(b"\n")
    sleep(SETTLE_SEC)
    port.reset_input_buffer()


def parse_who(line: str, port: str = "", baud: int = 0) -> Board | None:
    """``WHO`` の応答の行を読む.

    Args:
        line: ``[tx] who role=tx …`` か ``# who role=rx …``。
        port: 応答したポート。
        baud: そのときのボーレート [bit/s]。

    Returns:
        読めれば Board、``WHO`` の応答でなければ None。
    """
    m = _WHO.match(line.strip())
    if m is None:
        return None
    kv = dict(_KV.findall(m.group("body")))
    role = kv.get("role")
    mac = kv.get("mac", "")
    if role not in ("tx", "rx") or len(mac) != MAC_HEX_LEN:
        return None
    peer = kv.get("rx_mac" if role == "tx" else "tx_mac", "0" * MAC_HEX_LEN)
    paired_text = kv.get("paired")
    paired = None if paired_text is None else paired_text == "1"
    return Board(port=port, baud=baud, role=role, mac=mac.lower(), peer_mac=peer.lower(),
                 paired=paired, fw=kv.get("fw", "unknown"), who=line.strip())


def ask_who(port: SerialPort, port_name: str, baud: int,
            timeout_sec: float = WHO_TIMEOUT_SEC,
            clock: Callable[[], float] = time.monotonic) -> Board | None:
    """開いたポートに ``WHO`` を送り、応答を待つ（CSI の行などは読み捨てる）.

    Returns:
        応答があれば Board、時間内に無ければ None。
    """
    port.write(b"WHO\n")
    deadline = clock() + timeout_sec
    while clock() < deadline:
        board = parse_who(read_line(port), port_name, baud)
        if board is not None:
            return board
    return None


def identify(port: str, opener: Opener = open_no_reset,
             bauds: Sequence[int] = IDENTIFY_BAUDS,
             sleep: Callable[[float], None] = time.sleep,
             clock: Callable[[], float] = time.monotonic) -> Board | None:
    """ポートの先の M5Stack Basic を見分ける（リセットしない）.

    Args:
        port: ポート名。
        opener: ポートを開く関数（テスト用）。
        bauds: 試すボーレート [bit/s] の順。
        sleep: 待つ関数（テスト用）。
        clock: 時計（テスト用）。

    Returns:
        ``WHO`` に答えれば Board、どの速さでも答えなければ None。

    Raises:
        OSError: ポートを開けない。
    """
    for baud in bauds:
        s = opener(port, baud)
        try:
            flush_partial_line(s, sleep)
            board = ask_who(s, port, baud, clock=clock)
        finally:
            s.close()
        if board is not None:
            return board
    return None


def find_boards(ports: Sequence[PortInfo], opener: Opener = open_no_reset,
                sleep: Callable[[float], None] = time.sleep,
                clock: Callable[[], float] = time.monotonic
                ) -> tuple[list[Board], dict[str, str]]:
    """ポートを順に見分ける.

    Returns:
        (答えた M5Stack Basic, 答えなかったポート → 理由)。
    """
    boards: list[Board] = []
    silent: dict[str, str] = {}
    for p in ports:
        try:
            board = identify(p.device, opener=opener, sleep=sleep, clock=clock)
        except OSError as e:
            silent[p.device] = str(e)
            continue
        if board is None:
            silent[p.device] = "WHO に答えない"
        else:
            boards.append(board)
    return boards, silent


def pick_role(boards: Sequence[Board], role: str) -> Board | None:
    """役割が ``role`` の M5Stack Basic が 1 台だけならそれを返す（0 台・2 台以上なら None）."""
    found = [b for b in boards if b.role == role]
    return found[0] if len(found) == 1 else None
