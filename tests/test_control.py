"""csikit.control: チャンネル・送信出力の切り替え、組を消す命令."""

from __future__ import annotations

import pytest

from csikit import control, ports
from tests.fake_boards import RX_BAUD, TX_BAUD, FakeClock, FakeDevice, make_opener

T_MAC = "020000000001"
R_MAC = "020000000002"
KEY = "0" * 32


def board(role: str, port: str, device: FakeDevice) -> ports.Board:
    """WHO で見分けた体の Board を作る."""
    found = ports.parse_who(device.who(), port, TX_BAUD if role == "tx" else RX_BAUD)
    assert found is not None
    return found


def kwargs(devices: dict[str, FakeDevice | None]) -> dict[str, object]:
    """偽のポートと時計."""
    clock = FakeClock()
    return {"opener": make_opener(devices), "clock": clock, "sleep": clock.sleep}


def test_set_channel_sends_ch_to_receiver_and_reads_ack() -> None:
    """受信機に CH n を送り、了解の項目（relay=ok など）を返す."""
    rx = FakeDevice("rx", R_MAC, peer=T_MAC, key=KEY)
    reply = control.set_channel(board("rx", "p1", rx), 6, **kwargs({"p1": rx}))
    assert reply.ok and reply.fields["channel"] == "6" and reply.fields["relay"] == "ok"
    assert rx.received[-1] == "CH 6" and rx.channel == 6


def test_set_channel_reports_refusal_reason() -> None:
    """送信機に届かなければ ok=False と理由（tx_no_ack）を返す."""
    rx = FakeDevice("rx", R_MAC, peer=T_MAC, key=KEY, relay_fails="tx_no_ack")
    reply = control.set_channel(board("rx", "p1", rx), 6, **kwargs({"p1": rx}))
    assert not reply.ok and reply.reason == "tx_no_ack" and rx.channel == 11


@pytest.mark.parametrize("channel", [0, 14])
def test_set_channel_rejects_out_of_range_before_sending(channel: int) -> None:
    """範囲外は送る前に止める."""
    rx = FakeDevice("rx", R_MAC, peer=T_MAC, key=KEY)
    with pytest.raises(control.ControlError, match="1〜13"):
        control.set_channel(board("rx", "p1", rx), channel, **kwargs({"p1": rx}))
    assert rx.received == []


def test_set_channel_needs_receiver() -> None:
    """送信機に CH は送らない（2 台いっしょに変えるのは受信機の役目）."""
    tx = FakeDevice("tx", T_MAC, peer=R_MAC, key=KEY)
    with pytest.raises(control.ControlError, match="受信機"):
        control.set_channel(board("tx", "p0", tx), 6, **kwargs({"p0": tx}))


def test_set_tx_power_reads_actual_value() -> None:
    """TXPS の了解から、実際の出力（dbm=）を読む."""
    rx = FakeDevice("rx", R_MAC, peer=T_MAC, key=KEY)
    reply = control.set_tx_power(board("rx", "p1", rx), 8, **kwargs({"p1": rx}))
    assert reply.ok and rx.received[-1] == "TXPS 8"
    assert float(reply.fields["dbm"]) <= 8.0


@pytest.mark.parametrize("dbm", [1.5, 20.5])
def test_set_tx_power_rejects_out_of_range(dbm: float) -> None:
    """2〜20 dBm の外は送る前に止める."""
    rx = FakeDevice("rx", R_MAC, peer=T_MAC, key=KEY)
    with pytest.raises(control.ControlError, match="dBm"):
        control.set_tx_power(board("rx", "p1", rx), dbm, **kwargs({"p1": rx}))


def test_unpair_clears_pair_on_either_role() -> None:
    """PAIRCLEAR は送信機・受信機のどちらにも送れ、組が消える."""
    tx = FakeDevice("tx", T_MAC, peer=R_MAC, key=KEY)
    rx = FakeDevice("rx", R_MAC, peer=T_MAC, key=KEY)
    devices: dict[str, FakeDevice | None] = {"p0": tx, "p1": rx}
    assert control.unpair(board("tx", "p0", tx), **kwargs(devices)).ok
    assert control.unpair(board("rx", "p1", rx), **kwargs(devices)).ok
    assert not tx.paired and not rx.paired


def test_send_refuses_pairset() -> None:
    """鍵を含む PAIRSET はこの経路では送らない."""
    rx = FakeDevice("rx", R_MAC)
    with pytest.raises(control.ControlError, match="pair-only"):
        control.send(board("rx", "p1", rx), f"PAIRSET {T_MAC} {KEY}", **kwargs({"p1": rx}))
    assert rx.received == []


def test_send_times_out_without_reply() -> None:
    """応答が無ければ ControlError（待ち続けない）."""
    rx = FakeDevice("rx", R_MAC, has_pair_command=False)  # 古いファーム: 知らない命令は返す
    silent = FakeDevice("rx", R_MAC)
    silent.handle = lambda line: []  # type: ignore[method-assign]  # 何も答えない
    with pytest.raises(control.ControlError, match="応答が無い"):
        control.send(board("rx", "p1", rx), "CH 6", **kwargs({"p1": silent}))
