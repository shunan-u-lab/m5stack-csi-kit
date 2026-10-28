"""csikit.doctor: 点検の項目と直し方."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from csikit import doctor, flash, ports
from tests.fake_boards import FakeDevice

S_MAC = "020000000001"
R_MAC = "020000000002"
KEY = "00112233445566778899aabbccddeeff"
M5 = (0x1A86, 0x55D4)


def board(dev: FakeDevice, port: str) -> ports.Board:
    """偽の M5Stack Basic の WHO を読んだ Board."""
    b = ports.parse_who(dev.who(), port, dev.baud)
    assert b is not None
    return b


def make_env(tmp_path: Path, devices: dict[str, FakeDevice | None], *, images: bool = True,
             system: str = "Linux", tools: tuple[str, ...] = ("esptool", "pio"),
             rw: bool = True, vidpid: tuple[int, int] = M5) -> doctor.Env:
    """点検に渡す偽の環境."""
    if images:
        for role in ("tx", "rx"):
            path = flash.default_image(role, tmp_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\xe9")

    def identify(port: str) -> ports.Board | None:
        dev = devices[port]
        return None if dev is None else board(dev, port)

    infos = [ports.PortInfo(p, "USB Single Serial", *vidpid) for p in devices]
    return doctor.Env(which=lambda name: f"/usr/bin/{name}" if name in tools else None,
                      system=lambda: system, list_ports=lambda: infos, identify=identify,
                      can_rw=lambda dev: rw, dist_dir=tmp_path)


def paired_devices() -> dict[str, FakeDevice | None]:
    """組んだ送信機・受信機."""
    return {"p0": FakeDevice("tx", S_MAC, peer=R_MAC, key=KEY),
            "p1": FakeDevice("rx", R_MAC, peer=S_MAC, key=KEY)}


def ng(checks: list[doctor.Check]) -> list[doctor.Check]:
    """NG の項目だけ."""
    return [c for c in checks if c.level is doctor.Level.NG]


def test_all_good(tmp_path: Path) -> None:
    """道具・.bin・ポート・2 台・組が揃えば NG なし."""
    checks = doctor.run(make_env(tmp_path, paired_devices()))
    assert ng(checks) == []
    assert "問題はありません" in doctor.report(checks)
    assert any(c.name == "組" and c.level is doctor.Level.OK for c in checks)


def test_missing_tools_and_images_have_hints(tmp_path: Path) -> None:
    """esptool・.bin が無ければ、入れ方・作り方を出す。PlatformIO は無くても NG にしない."""
    checks = doctor.run(make_env(tmp_path, paired_devices(), images=False, tools=()))
    names = {c.name: c for c in ng(checks)}
    assert "uv tool install esptool" in names["esptool"].hint
    assert "make_dist.py" in names[".bin（tx）"].hint
    assert "PlatformIO" not in names


@pytest.mark.parametrize(("system", "word"), [("Linux", "Linux と USB デバイスを共有"),
                                              ("Windows", "CH343SER"), ("Darwin", "CP210x")])
def test_no_ports_gives_os_specific_hint(tmp_path: Path, system: str, word: str) -> None:
    """ポートが 1 つも見えなければ、OS ごとの直し方を出す."""
    checks = doctor.run(make_env(tmp_path, {}, system=system))
    port = next(c for c in ng(checks) if c.name == "USB のポート")
    assert word in port.hint


def test_linux_permission(tmp_path: Path) -> None:
    """Linux で読み書きできなければ dialout の手順を出す."""
    checks = doctor.run(make_env(tmp_path, paired_devices(), rw=False))
    assert any(c.name == "権限" and "dialout" in c.hint for c in ng(checks))


def test_unknown_chip_is_still_tried(tmp_path: Path) -> None:
    """M5Stack に見えるチップが無くても、全ポートに WHO を試す."""
    checks = doctor.run(make_env(tmp_path, paired_devices(), vidpid=(0x0403, 0x6001)))
    assert any(c.name == "M5Stack Basic" for c in ng(checks))
    assert any(c.name == "組" and c.level is doctor.Level.OK for c in checks)


def test_unflashed_board_and_missing_role(tmp_path: Path) -> None:
    """WHO に答えない M5Stack Basic は flash を、足りない役割は見つからないと出す."""
    checks = doctor.run(make_env(tmp_path, {"p0": FakeDevice("tx", S_MAC), "p1": None}))
    msgs = [(c.name, c.detail, c.hint) for c in ng(checks)]
    assert ("M5Stack Basic", "p1: WHO に答えない",
            "このキットのファームが入っていない。uv run csikit flash で書き込む") in msgs
    assert any(name == "受信機（R1）" and detail == "見つからない" for name, detail, _ in msgs)


def test_two_receivers(tmp_path: Path) -> None:
    """同じ役割が 2 台あれば NG."""
    devices: dict[str, FakeDevice | None] = {"p0": FakeDevice("rx", S_MAC),
                                             "p1": FakeDevice("rx", R_MAC)}
    checks = doctor.run(make_env(tmp_path, devices))
    assert any(c.name == "受信機（R1）" and c.detail == "2 台ある" for c in ng(checks))


@pytest.mark.parametrize(("tx_kw", "rx_kw", "word"), [
    ({}, {}, "未設定"),
    ({"peer": "0200000000ff", "key": KEY}, {"peer": S_MAC, "key": KEY}, "合わない"),
    ({"has_pair_command": False}, {"has_pair_command": False}, "ファームが古い"),
])
def test_pair_problems(tmp_path: Path, tx_kw: dict, rx_kw: dict, word: str) -> None:
    """組が未設定・相手違い・古いファームを見分ける."""
    devices: dict[str, FakeDevice | None] = {"p0": FakeDevice("tx", S_MAC, **tx_kw),
                                             "p1": FakeDevice("rx", R_MAC, **rx_kw)}
    checks = doctor.run(make_env(tmp_path, devices))
    pair = [c for c in ng(checks) if c.name == "組"]
    assert len(pair) == 1 and word in pair[0].detail


def test_version_mismatch_is_info(tmp_path: Path) -> None:
    """版の違いは情報として出す（NG にしない）."""
    devices = paired_devices()
    rx = devices["p1"]
    assert rx is not None
    devices["p1"] = replace(rx, fw="1001-0000000")
    checks = doctor.run(make_env(tmp_path, devices))
    assert ng(checks) == []
    assert any(c.name == "版" for c in checks)


def test_unopenable_port(tmp_path: Path) -> None:
    """開けないポートは、他のアプリを閉じる手順を出す."""
    env = make_env(tmp_path, paired_devices())

    def busy(port: str) -> ports.Board | None:
        raise OSError(f"{port} を開けません: busy")

    checks = doctor.run(replace(env, identify=busy))
    assert any("他のアプリ" in c.hint for c in ng(checks))
