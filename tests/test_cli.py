"""csikit.cli: ポートの決め方・確認・失敗の表示."""

from __future__ import annotations

import argparse

import pytest

from csikit import cli, ports
from tests.fake_boards import FakeDevice

S_MAC = "020000000001"
R_MAC = "020000000002"


def args(**kw: object) -> argparse.Namespace:
    """Flash の引数."""
    base = {"tx_port": None, "rx_port": None}
    base.update(kw)
    return argparse.Namespace(**base)


def patch_boards(monkeypatch: pytest.MonkeyPatch, devices: dict[str, FakeDevice]) -> None:
    """find_boards が偽の M5Stack Basic を返すようにする."""
    found = [b for p, d in devices.items() if (b := ports.parse_who(d.who(), p, d.baud))]
    monkeypatch.setattr(cli.ports, "list_ports", lambda: [])
    monkeypatch.setattr(cli.ports, "find_boards", lambda infos: (found, {}))


def test_explicit_ports_are_used_as_is() -> None:
    """両方指定すればそのまま使う。同じポートは断る."""
    assert cli.resolve_pair_ports(args(tx_port="COM3", rx_port="COM4")) == ("COM3", "COM4")
    with pytest.raises(cli.CliError, match="同じ"):
        cli.resolve_pair_ports(args(tx_port="COM3", rx_port="COM3"))


def test_ports_found_by_who(monkeypatch: pytest.MonkeyPatch) -> None:
    """指定が無ければ WHO で探す."""
    patch_boards(monkeypatch, {"p0": FakeDevice("rx", R_MAC), "p1": FakeDevice("tx", S_MAC)})
    assert cli.resolve_pair_ports(args()) == ("p1", "p0")


def test_unflashed_boards_need_explicit_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    """WHO に答えない（書き込み前の） M5Stack Basic は、ポートの指定を求める."""
    patch_boards(monkeypatch, {"p1": FakeDevice("tx", S_MAC)})
    with pytest.raises(cli.CliError, match="--rx-port"):
        cli.resolve_pair_ports(args())


def test_find_role_explains_what_was_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """受信機が見つからなければ doctor を案内する."""
    patch_boards(monkeypatch, {"p1": FakeDevice("tx", S_MAC)})
    with pytest.raises(cli.CliError, match="受信機が見つかりません.*csikit doctor"):
        cli.find_role("rx")


def test_confirm(monkeypatch: pytest.MonkeyPatch) -> None:
    """--yes なら聞かない。y 以外はやめる."""
    assert cli.confirm("?", assume_yes=True)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    assert cli.confirm("?", assume_yes=False)
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    assert not cli.confirm("?", assume_yes=False)


def test_errors_are_shown_with_exit_code_1(monkeypatch: pytest.MonkeyPatch,
                                           capsys: pytest.CaptureFixture[str]) -> None:
    """想定した失敗は、理由を表示して終了コード 1."""
    patch_boards(monkeypatch, {})
    assert cli.main(["record", "--duration", "1", "--label", "x"]) == 1
    assert "エラー: 受信機が見つかりません" in capsys.readouterr().err


def test_record_passes_port_and_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    """Record は既定でリセットせず、ポートと残りの引数を capture_csi に渡す."""
    seen: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(cli, "run_script", lambda name, argv: seen.append((name, argv)) or 0)
    assert cli.main(["record", "--port", "COM4", "--duration", "60", "--label", "empty",
                     "--no-meta"]) == 0
    assert seen == [("capture_csi", ["--duration", "60.0", "--label", "empty", "--port", "COM4",
                                     "--no-reset", "--no-meta"])]
    seen.clear()
    assert cli.main(["record", "--port", "COM4", "--duration", "1", "--label", "x", "--reset"]) == 0
    assert "--no-reset" not in seen[0][1]


def test_monitor_rejects_unknown_args(capsys: pytest.CaptureFixture[str]) -> None:
    """Monitor は知らない引数を断る（record と違い、渡す先が無い）."""
    with pytest.raises(SystemExit):
        cli.main(["monitor", "--bogus"])
    assert "知らない引数" in capsys.readouterr().err


def patch_control(monkeypatch: pytest.MonkeyPatch, devices: dict[str, FakeDevice]) -> None:
    """命令（csikit.control）が偽の M5Stack Basic に届くようにする（時計も偽物）."""
    from csikit import control
    from tests.fake_boards import FakeClock, make_opener

    clock = FakeClock()
    opener = make_opener(dict(devices))
    real_send = control.send
    monkeypatch.setattr(control, "send", lambda board, command, *a, **k: real_send(
        board, command, opener, clock, clock.sleep))


def test_channel_command(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
                         ) -> None:
    """Csikit channel 6: 受信機に CH 6 を送り、戻り方も伝える。断られたら直し方を出して 1."""
    rx = FakeDevice("rx", R_MAC, peer=S_MAC, key="0" * 32)
    devices = {"p0": FakeDevice("tx", S_MAC, peer=R_MAC, key="0" * 32), "p1": rx}
    patch_boards(monkeypatch, devices)
    patch_control(monkeypatch, devices)
    assert cli.main(["channel", "6"]) == 0
    out = capsys.readouterr().out
    assert "チャンネルを 6 にしました" in out and "ch11 に戻ります" in out
    rx.relay_fails = "tx_no_ack"
    assert cli.main(["channel", "1"]) == 1
    assert "送信機から了解が来ません" in capsys.readouterr().err
    assert cli.main(["channel", "14"]) == 1
    assert "1〜13" in capsys.readouterr().err


def test_txpower_command(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
                         ) -> None:
    """Csikit txpower 8: 実際の出力と、記録の途中で変えない注意を出す."""
    devices = {"p1": FakeDevice("rx", R_MAC, peer=S_MAC, key="0" * 32)}
    patch_boards(monkeypatch, devices)
    patch_control(monkeypatch, devices)
    assert cli.main(["txpower", "8"]) == 0
    out = capsys.readouterr().out
    assert "dBm にしました（指定 8 dBm" in out and "記録の途中では変えない" in out


def test_unpair_command_asks_then_clears_both(monkeypatch: pytest.MonkeyPatch,
                                              capsys: pytest.CaptureFixture[str]) -> None:
    """Csikit unpair: 確認を取り、つながっている 2 台の組を消す。n ならやめる."""
    tx = FakeDevice("tx", S_MAC, peer=R_MAC, key="0" * 32)
    rx = FakeDevice("rx", R_MAC, peer=S_MAC, key="0" * 32)
    devices = {"p0": tx, "p1": rx}
    patch_boards(monkeypatch, devices)
    patch_control(monkeypatch, devices)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert cli.main(["unpair"]) == 1 and tx.paired and rx.paired
    assert cli.main(["unpair", "--yes"]) == 0
    assert not tx.paired and not rx.paired
    assert "--pair-only" in capsys.readouterr().out
