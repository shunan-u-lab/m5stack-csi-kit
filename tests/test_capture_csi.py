"""scripts/capture_csi.py のうち、実機なしで確かめられる部分のテスト."""

from __future__ import annotations

import importlib.util
import io as stdio
import sys
import zlib
from datetime import datetime
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "capture_csi.py"
RAW_HEX = "00" * 256  # len=256 の CSI（中身は問わない）


def load_script() -> ModuleType:
    """scripts/ はパッケージではないので、ファイルから読み込む."""
    spec = importlib.util.spec_from_file_location("capture_csi", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["capture_csi"] = module
    spec.loader.exec_module(module)
    return module


cap = load_script()


def csi_line(rx_us: int, seq: int = 0) -> bytes:
    """CRC つきの正しい CSI 行（format=2）."""
    body = f"CSI,{rx_us},{seq},-52,-96,11,0,1,0,0,0,0,1,256,{RAW_HEX}"
    return f"{body}*{zlib.crc32(body.encode()):08x}".encode()


def test_assembler_joins_lines_split_across_chunks() -> None:
    """断片の境目で切れた行も 1 行に戻る."""
    asm = cap.LineAssembler()
    line = csi_line(1)
    assert asm.feed(b"# a\r\n" + line[:100]) == [b"# a"]
    assert asm.feed(line[100:] + b"\n") == [line]
    assert asm.feed(b"") == []


def test_handle_line_keeps_valid_and_comments_only() -> None:
    """正しい CSI 行と # 行だけを書き、壊れた行と起動ログは数えるだけ."""
    out = stdio.StringIO()
    stats = cap.CaptureStats()
    good = csi_line(10_000)
    for line in (b"ets Jun  8 2016", b"# stats rate_hz=99.0 dropped=0 truncated=0",
                 good, good[:-1] + b"0", good[:200]):
        cap.handle_line(line, out, stats)
    assert (stats.ok, stats.bad_crc, stats.bad_format, stats.other) == (1, 2, 0, 1)
    assert out.getvalue().splitlines() == ["# stats rate_hz=99.0 dropped=0 truncated=0",
                                           good.decode()]


def test_summary_rate_and_missing() -> None:
    """100 Hz で 1 個抜けた記録の集計."""
    stats = cap.CaptureStats(ok=4, rx_us=[0, 10_000, 30_000, 40_000])
    summary = stats.summary()
    assert "rate_hz=75.0" in summary  # 3 間隔 / 0.04 秒
    assert "est_missing=1" in summary


def test_warnings_detect_receiver_drops() -> None:
    """受信機の dropped が 0 でなければ警告し、0 なら警告しない."""
    rx_us = list(range(0, 1_000_000, 10_000))
    ok = cap.CaptureStats(ok=len(rx_us), rx_us=rx_us,
                          last_receiver_stats="# stats dropped=0 truncated=0")
    assert ok.warnings() == []
    dropped = cap.CaptureStats(ok=len(rx_us), rx_us=rx_us,
                               last_receiver_stats="# stats dropped=10 truncated=0")
    assert any("取りこぼし" in w for w in dropped.warnings())


def test_build_output_path(tmp_path: Path) -> None:
    """ラベルを検証し、既存ファイルは上書きしない."""
    now = datetime(2026, 9, 23, 9, 30, 0)
    path = cap.build_output_path(tmp_path, "empty", now)
    assert path.name == "20260923-093000-empty.csv"
    with pytest.raises(ValueError):
        cap.build_output_path(tmp_path, "../etc", now)
    path.write_text("", encoding="utf-8")
    with pytest.raises(FileExistsError):
        cap.build_output_path(tmp_path, "empty", now)


def test_main_rejects_missing_port(tmp_path: Path) -> None:
    """ポートが無ければ何も書かずに 1 を返す."""
    code = cap.main(["--duration", "1", "--label", "x", "--port", str(tmp_path / "nope"),
                     "--out-dir", str(tmp_path)])
    assert code == 1
    assert list(tmp_path.iterdir()) == []


def test_handle_line_counts_format_mismatch_separately() -> None:
    """CRC は正しいが列数の違う行は bad_format として数える（CRC 不一致と混ぜない）."""
    body = "CSI,1,2,3"
    line = f"{body}*{zlib.crc32(body.encode()):08x}".encode()
    stats = cap.CaptureStats()
    cap.handle_line(line, stdio.StringIO(), stats)
    assert (stats.bad_crc, stats.bad_format) == (0, 1)
    assert any("format" in w for w in stats.warnings())


def test_summary_counts_tx_restart_in_ascii() -> None:
    """送信機の再起動で連番が戻っても、区切りごとに欠番を数え、再起動の回数を ASCII で出す."""
    seq = [100, 101, 103, 0, 1, 2, 4]  # 103 の後に再起動。欠番は 102 と 3 の 2 つ
    stats = cap.CaptureStats(ok=len(seq), rx_us=[10_000 * i for i in range(len(seq))], seq=seq)
    summary = stats.summary()
    assert "missing_seq=2" in summary and "tx_restarts=1" in summary
    assert "seq_error" not in summary
    summary.encode("ascii")  # 記録ファイルに書くので ASCII だけ


def test_summary_reports_missing_by_seq() -> None:
    """連番から欠番を数えて集計に出す."""
    stats = cap.CaptureStats(ok=3, rx_us=[0, 10_000, 30_000], seq=[7, 8, 10])
    summary = stats.summary()
    assert "missing_seq=1" in summary and "seq_unknown=0" in summary


def test_check_meta_base(tmp_path: Path) -> None:
    """--meta-base は記録の前に確かめ、ない・壊れている・形が違うなら理由を返す."""
    good = (Path(__file__).resolve().parent.parent / "examples" / "meta"
            / "20260101-120000-static.toml")
    assert cap.check_meta_base(None) is None
    assert cap.check_meta_base(good) is None
    assert "読めません" in cap.check_meta_base(tmp_path / "none.toml")
    broken = tmp_path / "broken.toml"
    broken.write_text("schema = 2\n", encoding="utf-8")
    assert "形の問題" in cap.check_meta_base(broken)


def test_write_meta_draft_after_capture(tmp_path: Path) -> None:
    """記録の後に下書きを書き、同名があれば警告だけで None を返す."""
    import argparse

    csv = tmp_path / "20260923-150000-walk.csv"
    csv.write_text("\n".join(["# capture start=2026-09-23T15:00:00 port=x",
                              *(csi_line(i * 10_000).decode() for i in range(10))]) + "\n",
                   encoding="utf-8")
    args = argparse.Namespace(no_meta=False, meta_dir=tmp_path / "meta", experiment="exp04",
                              meta_base=None)
    out = cap.write_meta_draft(csv, args)
    assert out is not None and out.name == "20260923-150000-walk.toml"
    assert cap.write_meta_draft(csv, args) is None  # 上書きしない
    args.no_meta = True
    assert cap.write_meta_draft(csv, args) is None


def test_wait_before_start_counts_down() -> None:
    """待ち時間を 5 秒ごとに区切って待ち、残りを表示する（0 なら待たない）."""
    slept: list[float] = []
    out = stdio.StringIO()
    cap.wait_before_start(12.0, sleep=slept.append, out=out)
    assert slept == [5.0, 5.0, 2.0]
    assert out.getvalue().count("記録開始まで") == 3
    slept.clear()
    cap.wait_before_start(0.0, sleep=slept.append, out=out)
    assert slept == []


def test_main_rejects_negative_start_delay() -> None:
    """--start-delay が負なら何もせず終了コード 1."""
    assert cap.main(["--duration", "1", "--label", "x", "--start-delay", "-1"]) == 1


def test_summary_counts_missing_per_transmitter() -> None:
    """送信機 2 台の記録では、欠番を送信機ごとに数え、代表は行の多い送信機の値にする."""
    s_tx, p_tx = "02:00:00:00:00:0a", "02:00:00:00:00:0c"
    stats = cap.CaptureStats()
    for i in range(10):  # 主: 連番 0..9 のうち 5 が欠け
        if i != 5:
            stats.rx_us.append(i * 10_000)
            stats.seq.append(i)
            stats.tx.append(s_tx)
    for i in range(6):  # 2 台目: 連番 100..105、欠けなし
        stats.rx_us.append(i * 10_000 + 3000)
        stats.seq.append(100 + i)
        stats.tx.append(p_tx)
    stats.ok = len(stats.rx_us)
    summary = stats.summary()
    fields = dict(kv.split("=", 1) for kv in summary.split())
    assert fields["missing_seq"] == "1"  # 代表 = 主（9 行）
    assert fields["tx_02000000000a_missing_seq"] == "1"
    assert fields["tx_02000000000c_missing_seq"] == "0"
    assert "seq_error" not in summary  # 2 台の連番が混ざって逆行扱いにならない


def test_representative_is_primary_from_header() -> None:
    """見出しに主の送信機があれば、行が少なくてもそれを代表にする."""
    s_tx, p_tx = "02:00:00:00:00:0a", "02:00:00:00:00:0c"
    stats = cap.CaptureStats()
    cap.handle_line(b"# tx id=0 mac=02000000000a primary=1", stdio.StringIO(), stats)
    assert stats.primary_tx == s_tx
    for i in range(3):  # 主は 3 行（連番 1 つ欠け）
        stats.rx_us.append(i * 20_000)
        stats.seq.append(i * 2)
        stats.tx.append(s_tx)
    for i in range(8):  # 2 台目は 8 行（欠けなし）
        stats.rx_us.append(i * 10_000 + 1)
        stats.seq.append(i)
        stats.tx.append(p_tx)
    stats.ok = len(stats.rx_us)
    fields = dict(kv.split("=", 1) for kv in stats.summary().split())
    assert fields["missing_seq"] == "2"  # 主（0, 2, 4 → 1 と 3 が欠け）


def test_reset_receiver_flushes_during_and_after_reset() -> None:
    """リセット中と、解いた直後の 2 回、たまった行を捨てる（先頭にリセット前の行が残らない）."""
    calls: list[str] = []

    class FakeSerial:
        def setDTR(self, value: bool) -> None:  # noqa: N802 - pyserial の名前に合わせる
            calls.append(f"DTR={value}")

        def setRTS(self, value: bool) -> None:  # noqa: N802
            calls.append(f"RTS={value}")

        def reset_input_buffer(self) -> None:
            calls.append("flush")

    waits: list[float] = []
    cap.reset_receiver(FakeSerial(), sleep=waits.append)
    assert calls == ["DTR=False", "RTS=True", "flush", "RTS=False", "flush"]
    assert waits == [cap.RESET_PULSE_SEC, cap.STALE_FLUSH_SEC]


class FakeRx:
    """偽の受信機のポート（WHO に答え、その後は CSI 行を流す）."""

    WHO = (b"# who role=rx mac=020000000002 channel=11 format=3 tx_mac=020000000001 relay=1 "
           b"pico=0 auth=on tx_power_dbm=19.50 fw=1027-abcdef0 paired=1")

    def __init__(self, n_lines: int = 50) -> None:
        """n_lines: WHO の後に流す CSI 行の数."""
        self.pending = [b"CSI,partial-line-from-before-open\n"]  # 開いたときの途中の行
        self.lines = [csi_line(i * 10_000, seq=i) + b"\n" for i in range(n_lines)]
        self.written: list[bytes] = []
        self.dtr_reset = False

    def write(self, data: bytes) -> int:
        """WHO が来たら応答を積む."""
        self.written.append(data)
        if data == b"WHO\n":
            self.pending.append(self.WHO + b"\n")
            self.pending += self.lines
        return len(data)

    def readline(self) -> bytes:
        """1 行を返す."""
        return self.pending.pop(0) if self.pending else b""

    def read(self, n: int) -> bytes:
        """届いている分を返す."""
        data, self.pending = b"".join(self.pending), []
        return data

    def reset_input_buffer(self) -> None:
        """届いていた行を捨てる."""
        self.pending = []

    def setDTR(self, value: bool) -> None:  # noqa: N802  pyserial の名前に合わせる
        """リセットの線（呼ばれてはいけない）."""
        self.dtr_reset = True

    setRTS = setDTR  # noqa: N815

    def close(self) -> None:
        """閉じる."""

    def __enter__(self) -> FakeRx:
        """With 文で使う."""
        return self

    def __exit__(self, *exc: object) -> None:
        """With 文で使う."""


def test_no_reset_capture_records_who_for_metadata(tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """リセットしない記録は、DTR/RTS を下げて開き、途中の行を捨て、WHO の応答を記録に残す."""
    from csikit import meta

    fake = FakeRx()
    opened: list[tuple[str, int]] = []

    def open_no_reset(port: str, baud: int) -> FakeRx:
        opened.append((port, baud))
        return fake

    monkeypatch.setattr(cap.ports, "open_no_reset", open_no_reset)
    monkeypatch.setattr(cap.ports.time, "sleep", lambda sec: None)
    path = tmp_path / "20260101-120000-empty.csv"
    stats = cap.capture("COM4", 921600, 0.2, path, reset=False)
    assert opened == [("COM4", 921600)]
    assert fake.written[:2] == [b"\n", b"WHO\n"]  # 途中の行を終わらせてから WHO
    assert not fake.dtr_reset
    assert stats.ok == 50 and stats.bad_format == 0  # 開く前の途中の行は捨てた
    text = path.read_text(encoding="utf-8")
    assert "# who role=rx mac=020000000002" in text and "reset=False" in text
    info = meta.read_capture_info(path)
    assert (info.rx_mac, info.tx_mac, info.channel) == ("02:00:00:00:00:02",
                                                        "02:00:00:00:00:01", 11)


def test_reset_capture_opens_normally(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """リセットする記録は従来どおり開いてリセットし、WHO は送らない."""
    fake = FakeRx()
    monkeypatch.setattr(cap.serial, "Serial", lambda port, baud, timeout: fake)
    monkeypatch.setattr(cap.time, "sleep", lambda sec: None)
    fake.pending = [b"# CSI receiver format=3\n"]
    cap.capture("COM4", 921600, 0.1, tmp_path / "x.csv", reset=True)  # ここでは開き方だけを見る
    assert fake.dtr_reset and b"WHO\n" not in fake.written
