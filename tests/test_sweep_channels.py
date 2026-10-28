"""scripts/sweep_channels.py のテスト（実機の代わりに偽の M5Stack Basic を使う）."""

from __future__ import annotations

import importlib.util
import sys
import time
import zlib
from pathlib import Path
from types import ModuleType

import pytest

from csikit import io, meta

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "sweep_channels.py"
RAW_HEX = "00" * 256  # len=256 の CSI（中身は問わない）
RATE_HZ = 100.0
TX_MAC_HEX = "02000000000a"
RX_MAC_HEX = "02000000000b"
TX_MAC = "02:00:00:00:00:0a"
RX_MAC = "02:00:00:00:00:0b"


def load_script() -> ModuleType:
    """scripts/ はパッケージではないので、ファイルから読み込む."""
    spec = importlib.util.spec_from_file_location("sweep_channels", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["sweep_channels"] = module
    spec.loader.exec_module(module)
    return module


sw = load_script()


class FakeBoard:
    """ファームウェアと同じ形で WHO / CH に応答する偽の M5Stack Basic（受信機は CSI 行も流す）."""

    def __init__(self, role: str, mac_hex: str, baud: int, peer: FakeBoard | None = None,
                 ack: bool = True, relay: bool = True) -> None:
        """役割・MAC・速度を決める（受信機には送信機を渡し、同じチャンネルのときだけ受ける）.

        受信機の ``relay`` が真なら、CH を送信機にも伝える（無線の代わりに相手を変える）。
        """
        self.role, self.mac_hex, self.baud, self.peer, self.ack = role, mac_hex, baud, peer, ack
        self.relay = relay
        self.channel = sw.HOME_CHANNEL
        self.opened_baud = 0
        self.commands: list[str] = []
        self._in = b""
        self._out = b""
        self._t0 = time.monotonic()
        self._sent = 0  # 流した CSI 行の数

    def open(self, baud: int) -> FakeBoard:
        """開く（速度が違うと応答は化ける）."""
        self.opened_baud = baud
        return self

    def _reply(self, text: str) -> None:
        prefix = "[tx] " if self.role == "tx" else "# "
        self._out += (prefix + text + "\n").encode()

    def write(self, data: bytes) -> int:
        """命令を受け取って応答を積む."""
        if self.opened_baud != self.baud:
            return len(data)  # 速度違いでは届かない
        self._in += data
        *lines, self._in = self._in.split(b"\n")
        for raw in lines:
            line = raw.decode()
            self.commands.append(line)
            cmd, _, arg = line.partition(" ")
            if line == "WHO":
                extra = ""
                if self.role == "rx" and self.relay and self.peer is not None:
                    extra = f" format=2 tx_mac={self.peer.mac_hex} relay=1"
                self._reply(f"who role={self.role} mac={self.mac_hex} channel={self.channel}"
                            + extra)
            elif cmd in ("CH", "CHLOCAL"):
                ch = int(arg)
                if not 1 <= ch <= 13:
                    self._reply(f"err cmd={cmd} reason=out_of_range arg={ch} min=1 max=13")
                elif not self.ack:
                    pass
                elif cmd == "CH" and self.role == "rx" and self.relay and self.peer is not None:
                    self.peer.channel = ch  # 無線で伝えた代わり
                    self.channel = ch
                    self._reply(f"ack cmd=CH channel={ch} relay=ok acked=1 attempts=1")
                else:
                    self.channel = ch
                    self._reply(f"ack cmd={cmd} channel={ch}")
        return len(data)

    def read(self, size: int) -> bytes:
        """積んだ応答と、（受信機なら）経過時間分の CSI 行を返す."""
        time.sleep(0.002)
        if self.opened_baud != self.baud:
            return b"\xff\x00"  # 化けた文字
        if self.role == "rx":
            due = int((time.monotonic() - self._t0) * RATE_HZ)
            linked = self.peer is not None and self.peer.channel == self.channel
            while self._sent < due:
                if linked:
                    body = (f"CSI,{self._sent * 10_000},{self._sent},-55,-96,{self.channel},"
                            f"0,1,0,0,0,0,0,256,{RAW_HEX}")
                    self._out += f"{body}*{zlib.crc32(body.encode()):08x}\n".encode()
                self._sent += 1
        out, self._out = self._out[:size], self._out[size:]
        return out

    def close(self) -> None:
        """閉じる（何もしない）."""


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """起動待ちと応答待ちを短くする."""
    monkeypatch.setattr(sw, "BOOT_WAIT_SEC", 0.0)
    monkeypatch.setattr(sw, "REPLY_TIMEOUT_SEC", 0.3)


def make_boards(tx_ack: bool = True, relay: bool = True,
                ) -> tuple[FakeBoard, FakeBoard, dict[str, FakeBoard]]:
    """ポート 0 = 送信機、ポート 1 = 受信機（最初に試す速度は受信機の 921600）."""
    tx = FakeBoard("tx", TX_MAC_HEX, sw.TX_BAUD, ack=tx_ack)
    rx = FakeBoard("rx", RX_MAC_HEX, sw.RX_BAUD, peer=tx, relay=relay)
    return tx, rx, {"fake0": tx, "fake1": rx}


def test_discover_identifies_boards_by_who() -> None:
    """ポートの順や速度に関係なく、WHO の応答で送信機と受信機を見分ける."""
    tx, rx, ports = make_boards()
    found_tx, found_rx = sw.discover(["fake1", "fake0"], lambda p, b: ports[p].open(b),
                                     TX_MAC, RX_MAC)
    assert (found_tx.port, found_tx.baud, found_tx.mac) == ("fake0", sw.TX_BAUD,
                                                            TX_MAC)
    assert (found_rx.port, found_rx.baud) == ("fake1", sw.RX_BAUD)


def test_discover_rejects_unexpected_mac() -> None:
    """MAC が期待と違えば止める."""
    _, _, ports = make_boards()
    with pytest.raises(RuntimeError, match="MAC"):
        sw.discover(["fake0", "fake1"], lambda p, b: ports[p].open(b), "aa:bb:cc:dd:ee:ff",
                    RX_MAC)


def test_sweep_records_each_channel_and_returns_home(tmp_path: Path) -> None:
    """direct: 各チャンネルで送信機 → 受信機の順に切り替えて記録し、最後に ch11 に戻す."""
    tx, rx, ports = make_boards()
    argv = ["--mode", "direct", "--ports", "fake0", "fake1", "--channels", "1", "6",
            "--duration", "0.3",
            "--settle", "0.05", "--out-dir", str(tmp_path / "raw"),
            "--meta-dir", str(tmp_path / "meta")]
    assert sw.main(argv, opener=lambda p, b: ports[p].open(b)) == 0
    files = sorted((tmp_path / "raw").glob("*.csv"))
    assert [f.stem.split("-", 2)[2] for f in files] == ["exp05b-s1-ch01", "exp05b-s1-ch06"]
    for f, ch in zip(files, (1, 6), strict=True):
        cap = io.load_rx_csv(f)
        assert 15 <= len(cap.time_sec) <= 40  # 0.3 秒 × 100 Hz 前後
        rows = [line.split(",") for line in f.read_text(encoding="utf-8").splitlines()
                if line.startswith("CSI,")]
        assert {r[sw.CSI_CHANNEL_FIELD] for r in rows} == {str(ch)}
        draft = meta.load_meta(tmp_path / "meta" / f"{f.stem}.toml")
        assert draft["radio"]["channel"] == ch and draft["experiment"] == "exp05b"
        assert {d["role"]: d["mac"] for d in draft["devices"]} == {
            "tx": TX_MAC, "rx": RX_MAC}
    assert tx.channel == rx.channel == sw.HOME_CHANNEL
    assert [c for c in tx.commands if c.startswith("CH")] == ["CH 1", "CH 6", "CH 11"]
    assert [c for c in rx.commands if c.startswith("CH")] == ["CHLOCAL 1", "CHLOCAL 6",
                                                             "CHLOCAL 11"]


def test_sweep_aborts_when_ack_missing_and_still_returns(tmp_path: Path) -> None:
    """送信機が ack を返さなければ中止し（終了コード 1）、記録は作らない."""
    _, rx, ports = make_boards(tx_ack=False)
    argv = ["--mode", "direct", "--ports", "fake0", "fake1", "--channels", "3", "--duration", "0.2",
            "--out-dir", str(tmp_path / "raw"), "--meta-dir", str(tmp_path / "meta")]
    assert sw.main(argv, opener=lambda p, b: ports[p].open(b)) == 1
    assert not (tmp_path / "raw").exists() or not list((tmp_path / "raw").glob("*.csv"))
    assert "CH 11" in ports["fake0"].commands  # 戻そうとした（送信機から）


@pytest.mark.parametrize("extra", [
    ["--channels", "14"],
    ["--channels", "1", "1"],
    ["--sweeps", "0"],
    ["--duration", "0"],
    ["--home-channel", "0"],
    ["--label-prefix", "x" * 40],
])
def test_bad_arguments_are_rejected_before_opening(extra: list[str]) -> None:
    """引数の誤りはポートを開く前に弾く."""
    def never(port: str, baud: int) -> FakeBoard:
        raise AssertionError("ポートを開いてはいけない")

    assert sw.main(["--ports", "fake0", *extra], opener=never) == 1


def test_warns_when_meta_base_has_todo(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--meta-base に TODO が残っていれば、掃引の前に警告する（止めはしない）."""
    _, _, ports = make_boards()
    # リポジトリの元ファイルは記入が進むので使わず、TODO を含む元をここで作る
    repo_base = Path(__file__).resolve().parent.parent / "examples" / "meta-base.toml"
    base_data = meta.load_meta(repo_base)
    base_data["placement"]["room"] = meta.TODO
    base = tmp_path / "base.toml"
    base.write_text(meta.dumps_meta(base_data), encoding="utf-8")
    argv = ["--ports", "fake0", "fake1", "--channels", "1", "--duration", "0.1",
            "--out-dir", str(tmp_path / "raw"), "--meta-dir", str(tmp_path / "meta"),
            "--meta-base", str(base)]
    assert sw.main(argv, opener=lambda p, b: ports[p].open(b)) == 0
    assert "--meta-base に TODO が" in capsys.readouterr().err


def test_relay_uses_only_receiver_and_moves_both(tmp_path: Path) -> None:
    """relay: 受信機だけに CH を送り、送信機のポートは使わずに両方が切り替わって、最後に ch11."""
    tx, rx, ports = make_boards()
    argv = ["--ports", "fake1", "--channels", "2", "9", "--duration", "0.3", "--settle", "0.05",
            "--out-dir", str(tmp_path / "raw"), "--meta-dir", str(tmp_path / "meta")]
    assert sw.main(argv, opener=lambda p, b: ports[p].open(b)) == 0
    files = sorted((tmp_path / "raw").glob("*.csv"))
    assert len(files) == 2
    for f, ch in zip(files, (2, 9), strict=True):
        rows = [line.split(",") for line in f.read_text(encoding="utf-8").splitlines()
                if line.startswith("CSI,")]
        assert rows and {r[sw.CSI_CHANNEL_FIELD] for r in rows} == {str(ch)}
        assert "mode=relay" in f.read_text(encoding="utf-8")
    assert [c for c in rx.commands if c.startswith("CH")] == ["CH 2", "CH 9", "CH 11"]
    assert tx.commands == []  # 送信機のシリアルには何も送らない
    assert tx.channel == rx.channel == sw.HOME_CHANNEL


def test_relay_rejects_old_receiver_firmware(tmp_path: Path) -> None:
    """Relay: 受信機が無線の命令に対応していなければ、記録せずに止める."""
    _, _, ports = make_boards(relay=False)
    argv = ["--ports", "fake1", "--channels", "2", "--out-dir", str(tmp_path / "raw"),
            "--meta-dir", str(tmp_path / "meta")]
    assert sw.main(argv, opener=lambda p, b: ports[p].open(b)) == 1
    assert not (tmp_path / "raw").exists()


def test_relay_ignores_transmitter_port_if_connected() -> None:
    """Relay: 送信機もつながっていれば、見つけても使わない（閉じる）."""
    _, _, ports = make_boards()
    tx, rx = sw.discover(["fake0", "fake1"], lambda p, b: ports[p].open(b), TX_MAC,
                         RX_MAC, mode="relay")
    assert tx is None and rx.relay and rx.tx_mac == TX_MAC


def test_discover_without_expected_macs_checks_pairing() -> None:
    """MAC を指定しなければ照合せず、受信機の組の相手がつながっている送信機かだけを見る."""
    _, _, ports = make_boards()
    tx, rx = sw.discover(["fake0", "fake1"], lambda p, b: ports[p].open(b))
    assert tx is not None and rx.tx_mac == tx.mac


def test_discover_rejects_receiver_paired_with_other_transmitter() -> None:
    """受信機が別の送信機と組んでいれば止め、組ませ直しを案内する."""
    other = FakeBoard("tx", "02000000000c", sw.TX_BAUD)
    tx = FakeBoard("tx", TX_MAC_HEX, sw.TX_BAUD)
    rx = FakeBoard("rx", RX_MAC_HEX, sw.RX_BAUD, peer=other, relay=True)
    ports = {"fake0": tx, "fake1": rx}
    with pytest.raises(RuntimeError, match="pair-only"):
        sw.discover(["fake0", "fake1"], lambda p, b: ports[p].open(b))
