"""csikit.ports: ポートの一覧・WHO の読み取り・役割の見分け."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from csikit import ports
from tests.fake_boards import RX_BAUD, TX_BAUD, FakeClock, FakeDevice, make_opener

S_MAC = "020000000001"
R_MAC = "020000000002"


def test_parse_who_of_both_roles() -> None:
    """送信機・受信機の WHO の行から、役割・MAC・相手・組・版を読む."""
    tx = ports.parse_who(FakeDevice("tx", S_MAC, peer=R_MAC, key="0" * 32).who(), "p0", TX_BAUD)
    assert tx is not None
    assert (tx.role, tx.mac, tx.peer_mac, tx.paired, tx.fw) == ("tx", S_MAC, R_MAC, True,
                                                                "1027-abcdef0")
    rx = ports.parse_who(FakeDevice("rx", R_MAC).who(), "p1", RX_BAUD)
    assert rx is not None
    assert (rx.role, rx.peer_mac, rx.paired, rx.baud) == ("rx", "0" * 12, False, RX_BAUD)


def test_parse_who_without_pair_field_is_old_firmware() -> None:
    """paired= の無い応答（古いファーム）は paired=None."""
    board = ports.parse_who(FakeDevice("rx", R_MAC, has_pair_command=False).who())
    assert board is not None and board.paired is None


@pytest.mark.parametrize("line", ["", "CSI,1,2,3", "# who role=xx mac=020000000001",
                                  "# who role=rx mac=0200", "[tx] seq=100 rate_hz=100.0"])
def test_parse_who_rejects_other_lines(line: str) -> None:
    """WHO の応答でない行・形の違う行は None."""
    assert ports.parse_who(line) is None


def test_list_ports_puts_m5stack_first_and_drops_builtin() -> None:
    """M5Stack Basic の USB 変換チップを先に並べ、USB でない内蔵のシリアルは除く."""
    raw = [SimpleNamespace(device="/dev/ttyUSB9", description="other", vid=0x0403, pid=0x6001),
           SimpleNamespace(device="/dev/ttyS0", description="builtin", vid=None, pid=None),
           SimpleNamespace(device="COM7", description="old", vid=0x10C4, pid=0xEA60),
           SimpleNamespace(device="/dev/ttyACM0", description="new", vid=0x1A86, pid=0x55D4)]
    found = ports.list_ports(lambda: raw)
    assert [p.device for p in found] == ["/dev/ttyACM0", "COM7", "/dev/ttyUSB9"]
    assert [p.chip for p in found] == ["CH9102", "CP2104", None]


def test_identify_tries_both_bauds_and_closes() -> None:
    """送信機は 921600 bps では化けるので、115200 bps で見分ける。開いたポートはすべて閉じる."""
    opened: list = []
    opener = make_opener({"p0": FakeDevice("tx", S_MAC)}, opened)
    clock = FakeClock()
    board = ports.identify("p0", opener=opener, sleep=clock.sleep, clock=clock)
    assert board is not None and (board.role, board.baud) == ("tx", TX_BAUD)
    assert [p.baud for p in opened] == [RX_BAUD, TX_BAUD]
    assert all(p.closed for p in opened)


def test_identify_flushes_partial_line_first() -> None:
    """WHO の前に改行を送り、開いたときに残っていた行を捨てる."""
    dev = FakeDevice("rx", R_MAC)
    clock = FakeClock()
    assert ports.identify("p1", opener=make_opener({"p1": dev}), sleep=clock.sleep, clock=clock)
    assert dev.received[:2] == ["", "WHO"]


def test_find_boards_reports_silent_and_unopenable_ports() -> None:
    """答えない・開けないポートは理由つきで分ける."""
    infos = [ports.PortInfo(d, "", 0x1A86, 0x55D4) for d in ("p0", "p1", "p2", "gone")]
    opener = make_opener({"p0": FakeDevice("tx", S_MAC), "p1": FakeDevice("rx", R_MAC),
                          "p2": None})
    clock = FakeClock()
    boards, silent = ports.find_boards(infos, opener=opener, sleep=clock.sleep, clock=clock)
    assert [(b.port, b.role) for b in boards] == [("p0", "tx"), ("p1", "rx")]
    assert silent["p2"] == "WHO に答えない"
    assert "開けません" in silent["gone"]


def test_pick_role_needs_exactly_one() -> None:
    """役割の M5Stack Basic が 1 台だけのときに返す."""
    tx = ports.parse_who(FakeDevice("tx", S_MAC).who(), "p0")
    tx2 = ports.parse_who(FakeDevice("tx", R_MAC).who(), "p1")
    assert tx is not None and tx2 is not None
    assert ports.pick_role([tx], "tx") == tx
    assert ports.pick_role([tx, tx2], "tx") is None
    assert ports.pick_role([tx], "rx") is None


def test_port_missing_only_judges_dev_paths() -> None:
    """Windows の COM ポートはファイルでないので、無いと決めない."""
    assert not ports.port_missing("COM3")
    assert ports.port_missing("/dev/ttyNOSUCH99")


class _RecordingSerial:
    """serial.Serial の代わり。開いた後の DTR/RTS の変更を順に記録する."""

    log: list[str] = []

    def __init__(self, port: str, baud: int, timeout: float) -> None:
        """普通に開く（DTR/RTS はカーネルが上げた状態）."""
        self.log.append("open")

    def __setattr__(self, name: str, value: object) -> None:
        """dtr・rts への代入を記録する."""
        if name in ("dtr", "rts"):
            self.log.append(f"{name}={value}")
        object.__setattr__(self, name, value)


def test_open_no_reset_lowers_rts_before_dtr(monkeypatch: pytest.MonkeyPatch) -> None:
    """開いてから RTS → DTR の順に下げる（DTR を先に下げる・開く前に決める形は再起動させる）."""
    import serial

    _RecordingSerial.log = []
    monkeypatch.setattr(serial, "Serial", _RecordingSerial)
    ports.open_no_reset("/dev/ttyACM9", RX_BAUD)
    assert _RecordingSerial.log == ["open", "rts=False", "dtr=False"]
