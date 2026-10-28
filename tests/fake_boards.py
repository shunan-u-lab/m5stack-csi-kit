"""テスト用の偽の M5Stack Basic（ファームの WHO・PAIRSET・CH・TXPS・PAIRCLEAR の応答をまねる）."""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

TX_BAUD = 115200  # [bit/s]
RX_BAUD = 921600  # [bit/s]
ZERO_MAC = "0" * 12
_PAIRSET = re.compile(r"^PAIRSET (\S+) (\S+)$")
_HEX12 = re.compile(r"^[0-9a-fA-F]{12}$")
_HEX32 = re.compile(r"^[0-9a-fA-F]{32}$")
_CH = re.compile(r"^CH (\d+)$")
_TXPS = re.compile(r"^TXPS (\S+)$")
TX_POWER_STEP_DBM = 0.25  # 送信出力の段 [dBm]（実物は指定より低い段に丸める）


@dataclass
class FakeDevice:
    """偽の M5Stack Basic 1 台（組は PAIRSET の直後に効く＝再起動を済ませたことにする）."""

    role: str
    mac: str
    peer: str = ZERO_MAC
    key: str | None = None
    fw: str = "1027-abcdef0"
    has_pair_command: bool = True  # False = 組の命令が無い古いファーム
    refuse_pairset: str | None = None  # 断る理由（None なら受け付ける）
    answers_who: bool = True
    channel: int = 11
    tx_power_dbm: float = 19.5  # 受信機なら「送信機の出力」として扱う
    relay_fails: str | None = None  # CH・TXPS で送信機に届かないときの理由（例: tx_no_ack）
    received: list[str] = field(default_factory=list)

    @property
    def baud(self) -> int:
        """この M5Stack Basic のボーレート [bit/s]."""
        return TX_BAUD if self.role == "tx" else RX_BAUD

    @property
    def paired(self) -> bool:
        """組が設定済みか."""
        return self.key is not None and self.peer != ZERO_MAC

    def who(self) -> str:
        """WHO の応答の行."""
        auth = "on" if self.paired else "off_no_key"
        if self.role == "tx":
            line = (f"[tx] who role=tx mac={self.mac} channel=11 auth={auth} "
                    f"tx_power_dbm=19.50 fw={self.fw}")
            return line + (f" paired={int(self.paired)} rx_mac={self.peer}"
                           if self.has_pair_command else "")
        line = (f"# who role=rx mac={self.mac} channel=11 format=3 tx_mac={self.peer} relay=1 "
                f"pico=0 auth={auth} tx_power_dbm=19.50 fw={self.fw}")
        return line + (f" paired={int(self.paired)}" if self.has_pair_command else "")

    def handle(self, line: str) -> list[str]:
        """1 行の命令に応答する."""
        self.received.append(line)
        prefix = "[tx]" if self.role == "tx" else "#"
        if line == "":
            return []
        if line == "WHO":
            return [self.who()] if self.answers_who else []
        m = _PAIRSET.match(line)
        if m and self.has_pair_command:
            if self.refuse_pairset:
                return [f"{prefix} err cmd=PAIRSET reason={self.refuse_pairset}"]
            mac, key = m.groups()
            if not _HEX12.match(mac) or mac == ZERO_MAC:
                return [f"{prefix} err cmd=PAIRSET reason=bad_mac"]
            if not _HEX32.match(key):
                return [f"{prefix} err cmd=PAIRSET reason=bad_key"]
            self.peer, self.key = mac.lower(), key.lower()
            return [f"{prefix} ack cmd=PAIRSET restarting"]
        if line == "PAIRCLEAR" and self.has_pair_command:
            self.peer, self.key = ZERO_MAC, None
            return [f"{prefix} ack cmd=PAIRCLEAR restarting"]
        m = _CH.match(line)
        if m and self.role == "rx":
            ch = int(m.group(1))
            if not 1 <= ch <= 13:
                return [f"# err cmd=CH reason=out_of_range arg={ch} min=1 max=13"]
            if self.relay_fails:
                return [f"# err cmd=CH reason={self.relay_fails} channel={ch} attempts=3 pico=off"]
            self.channel = ch
            return [f"# ack cmd=CH channel={ch} relay=ok acked=1 attempts=1 pico=off detour=0"]
        m = _TXPS.match(line)
        if m and self.role == "rx":
            want = float(m.group(1))
            if self.relay_fails:
                return [f"# err cmd=TXPS reason=lost_link want_dbm={want:.2f} attempts=3 "
                        "reverted=1"]
            self.tx_power_dbm = (want // TX_POWER_STEP_DBM) * TX_POWER_STEP_DBM - TX_POWER_STEP_DBM
            return [f"# ack cmd=TXPS want_dbm={want:.2f} dbm={self.tx_power_dbm:.2f} attempts=1"]
        return [f"{prefix} err cmd={line} reason=unknown"]  # 実物と同じく行をそのまま返す


class FakePort:
    """偽のシリアルポート（違うボーレートで開くと応答は化けて届く）."""

    def __init__(self, device: FakeDevice | None, baud: int,
                 noise: list[str] | None = None) -> None:
        """開いた直後に届く行（noise）を積んでおく."""
        self.device = device
        self.baud = baud
        self.out: deque[bytes] = deque(line.encode() + b"\n" for line in (noise or []))
        self.closed = False
        self.buf = b""

    def write(self, data: bytes) -> int:
        """命令を受け取り、応答を積む."""
        self.buf += data
        while b"\n" in self.buf:
            raw, self.buf = self.buf.split(b"\n", 1)
            if self.device is None:
                continue
            for reply in self.device.handle(raw.decode()):
                self.out.append(reply.encode() + b"\n" if self.baud == self.device.baud
                                else b"\xff\xfe garbage\n")
        return len(data)

    def readline(self) -> bytes:
        """1 行を返す（無ければ空）."""
        return self.out.popleft() if self.out else b""

    def reset_input_buffer(self) -> None:
        """届いていた行を捨てる."""
        self.out.clear()

    def close(self) -> None:
        """閉じる."""
        self.closed = True


class FakeClock:
    """呼ぶたびに進む時計（待ちのループをすぐ終わらせる）."""

    def __init__(self, step: float = 0.1) -> None:
        """step: 1 回呼ぶごとに進む時間 [sec]."""
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        """今の時刻 [sec]."""
        self.now += self.step
        return self.now

    def sleep(self, sec: float) -> None:
        """待つ代わりに時計を進める."""
        self.now += sec


def make_opener(devices: dict[str, FakeDevice | None],
                opened: list[FakePort] | None = None) -> Callable[[str, int], FakePort]:
    """ポート名 → 偽の M5Stack Basic から、ports.Opener を作る（無いポートは OSError）."""
    def opener(port: str, baud: int) -> FakePort:
        if port not in devices:
            raise OSError(f"{port} を開けません")
        p = FakePort(devices[port], baud, noise=["CSI,123,partial"])
        if opened is not None:
            opened.append(p)
        return p
    return opener
