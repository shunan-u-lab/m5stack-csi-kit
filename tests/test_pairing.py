"""csikit.pairing: 鍵の生成・PAIRSET・再起動後の確認."""

from __future__ import annotations

import pytest

from csikit import pairing, ports
from tests.fake_boards import FakeClock, FakeDevice, make_opener

S_MAC = "020000000001"
R_MAC = "020000000002"
KEY = "00112233445566778899aabbccddeeff"


def setup_pair(**rx_kw: object) -> tuple[FakeDevice, FakeDevice, dict]:
    """偽の送信機・受信機と、pair に渡す引数."""
    tx_dev, rx_dev = FakeDevice("tx", S_MAC), FakeDevice("rx", R_MAC, **rx_kw)  # type: ignore[arg-type]
    devices = {"p0": tx_dev, "p1": rx_dev}
    opener = make_opener(devices)
    clock = FakeClock()

    def identify(port: str) -> ports.Board | None:
        return ports.identify(port, opener=opener, sleep=clock.sleep, clock=clock)

    kw = {"opener": opener, "identify": identify, "clock": clock, "sleep": clock.sleep}
    return tx_dev, rx_dev, kw


def boards(kw: dict) -> tuple[ports.Board, ports.Board]:
    """両方を WHO で見分ける."""
    tx, rx = kw["identify"]("p0"), kw["identify"]("p1")
    assert tx is not None and rx is not None
    return tx, rx


def test_new_key_is_16_bytes_hex() -> None:
    """鍵は 16 進 32 文字で、毎回違う."""
    a, b = pairing.new_key(), pairing.new_key()
    assert len(a) == 32 and int(a, 16) >= 0 and a != b


@pytest.mark.parametrize(("mac", "key"), [("0200000000", KEY), ("0" * 12, KEY),
                                          ("02000000000g", KEY), (S_MAC, "00" * 15),
                                          (S_MAC, KEY.upper())])
def test_pairset_command_rejects_bad_values(mac: str, key: str) -> None:
    """MAC・鍵の形が違えば ValueError。文に鍵を含めない."""
    with pytest.raises(ValueError) as e:
        pairing.pairset_command(mac, key)
    assert key not in str(e.value)


def test_pair_sets_each_side_to_the_other() -> None:
    """送信機には受信機の MAC、受信機には送信機の MAC を、同じ鍵で置く."""
    tx_dev, rx_dev, kw = setup_pair()
    tx, rx = boards(kw)
    result = pairing.pair(tx, rx, key=KEY, **kw)
    assert (tx_dev.peer, rx_dev.peer) == (R_MAC, S_MAC)
    assert tx_dev.key == rx_dev.key == KEY
    assert result.tx.paired and result.rx.paired
    assert (result.tx.peer_mac, result.rx.peer_mac) == (R_MAC, S_MAC)


def test_pair_makes_a_new_key_when_not_given() -> None:
    """鍵を与えなければ作り、両方に同じものを置く."""
    tx_dev, rx_dev, kw = setup_pair()
    pairing.pair(*boards(kw), **kw)
    assert tx_dev.key is not None and tx_dev.key == rx_dev.key and len(tx_dev.key) == 32


def test_pair_refusal_is_reported_without_the_key() -> None:
    """M5Stack Basic が断ったら PairError。理由は出すが鍵は出さない."""
    _, _, kw = setup_pair(refuse_pairset="nvs_write")
    with pytest.raises(pairing.PairError) as e:
        pairing.pair(*boards(kw), key=KEY, **kw)
    assert "nvs_write" in str(e.value) and KEY not in str(e.value)


def test_pair_refuses_old_firmware_before_sending_the_key() -> None:
    """WHO に paired= が無い古いファームには、鍵を送らずに断る."""
    _, rx_dev, kw = setup_pair(has_pair_command=False)
    with pytest.raises(pairing.PairError, match="組の命令を知りません"):
        pairing.pair(*boards(kw), key=KEY, **kw)
    assert not any(KEY in line for line in rx_dev.received)


def test_echoed_command_does_not_leak_the_key() -> None:
    """古いファームは知らない命令の行をそのまま返す。そうなっても例外の文に鍵を出さない."""
    _, _, kw = setup_pair()
    tx, _ = boards(kw)
    old = FakeDevice("tx", S_MAC, has_pair_command=False)
    opener = make_opener({"p0": old})
    with pytest.raises(pairing.PairError) as e:
        pairing.send_pairset(tx, R_MAC, KEY, opener, kw["clock"], kw["sleep"])
    assert "reason=unknown" in str(e.value) and KEY not in str(e.value)


def test_pair_rejects_swapped_roles() -> None:
    """送信機と受信機を逆に渡したら、何も送らずに断る."""
    tx_dev, rx_dev, kw = setup_pair()
    tx, rx = boards(kw)
    with pytest.raises(pairing.PairError, match="役割が違う"):
        pairing.pair(rx, tx, key=KEY, **kw)
    assert tx_dev.key is None and rx_dev.key is None


def test_wait_paired_detects_wrong_peer() -> None:
    """再起動後の相手の MAC が違えば PairError."""
    tx_dev, _, kw = setup_pair()
    tx, _ = boards(kw)
    tx_dev.peer, tx_dev.key = "0200000000ff", KEY
    with pytest.raises(pairing.PairError, match="相手の MAC が違う"):
        pairing.wait_paired(tx, R_MAC, kw["identify"], kw["clock"], kw["sleep"])


def test_wait_role_detects_swapped_ports() -> None:
    """書き込み後、期待と違う役割が答えたら PairError."""
    _, _, kw = setup_pair()
    with pytest.raises(pairing.PairError, match="rx として答えた"):
        pairing.wait_role("p1", "tx", kw["identify"], kw["clock"], kw["sleep"])


def test_wait_role_times_out_on_silent_port() -> None:
    """答えないまま時間が過ぎたら PairError."""
    clock = FakeClock(step=5.0)
    with pytest.raises(pairing.PairError, match="答えない"):
        pairing.wait_role("p9", "rx", lambda port: None, clock, clock.sleep)
