"""scripts/sd_fetch.py のテスト（受信機の代わりをする偽物のポートで）."""

from __future__ import annotations

import argparse
import importlib.util
import sys
import types
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "sd_fetch.py"
CHUNK = 4096


def load_script() -> ModuleType:
    """scripts/ はパッケージではないので、ファイルから読み込む."""
    spec = importlib.util.spec_from_file_location("sd_fetch", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["sd_fetch"] = module
    spec.loader.exec_module(module)
    return module


sf = load_script()


class FakeReceiver:
    """受信機の SD の命令に答える偽物のポート（CSI 行も混ぜる。1 塊ごとに返事を待つ）.

    Attributes:
        files: SD のファイル（名前 → 中身）。
        corrupt_once: 1 回だけ壊して送る塊の位置（None なら壊さない）。
    """

    def __init__(self, files: dict[str, bytes], corrupt_once: int | None = None,
                 events: list[str] | None = None) -> None:
        """ファイルと、壊す塊と、NVS の出来事を決める."""
        self.files = files
        self.events = list(events or [])
        self.free_ok = True  # SDRM で空きが消した分だけ増えるか
        self.corrupt_once = corrupt_once
        self.buf = bytearray(b"CSI,1,2,3*00000000\n")
        self.commands: list[str] = []
        self.name = ""
        self.pos = 0

    def _send_chunk(self) -> None:
        body = self.files[self.name]
        if self.pos >= len(body):
            self.buf += f"# sd get done name={self.name} bytes={len(body)}\n".encode()
            return
        data = body[self.pos:self.pos + CHUNK]
        crc = zlib.crc32(data)
        if self.corrupt_once == self.pos:
            data = b"X" + data[1:]
            self.corrupt_once = None
        self.buf += f"#SDC {self.pos} {len(data)} {crc:08x}\n".encode() + data

    def write(self, data: bytes) -> int:
        """命令・返事を受けて、応答を読み出し口に積む."""
        cmd = data.decode().strip()
        self.commands.append(cmd)
        if cmd == "SDLS":
            for name, body in self.files.items():
                self.buf += f"# sd file name={name} bytes={len(body)}\n".encode()
            self.buf += b"CSI,9*0\n# sd ls done\n"
        elif cmd.startswith("SDGET "):
            _, self.name, off = cmd.split()
            self.pos = int(off)
            size = len(self.files[self.name])
            head = f"# sd get name={self.name} size={size} offset={self.pos} chunk={CHUNK}\n"
            self.buf += head.encode()
            self._send_chunk()
        elif cmd.startswith("SDRM "):
            name = cmd.split()[1]
            size = len(self.files.pop(name))
            freed = size if self.free_ok else 0
            after_mb = 100 + freed // 2**20
            self.buf += (f"# ack cmd=SDRM name={name} bytes={size} free_before_mb=100 "
                         f"free_after_mb={after_mb} freed_mb={freed // 2**20}\n").encode()
        elif cmd in ("SDEVENTS", "SDEVENTS CLEAR"):
            self.buf += f"# sdevents count={len(self.events)} total={len(self.events)}\n".encode()
            for ev in self.events:
                self.buf += b"CSI,9*0\n" + ev.encode() + b"\n"
            if cmd == "SDEVENTS CLEAR":
                self.events.clear()
                self.buf += b"# sdevents cleared\n"
        elif cmd == "K":
            self.pos += CHUNK
            self._send_chunk()
        elif cmd == "R":
            self._send_chunk()
        return len(data)

    def readline(self) -> bytes:
        """改行までを返す."""
        i = self.buf.find(b"\n")
        if i < 0:
            out, self.buf = bytes(self.buf), bytearray()
            return out
        out = bytes(self.buf[:i + 1])
        del self.buf[:i + 1]
        return out

    def read(self, size: int) -> bytes:
        """読み出し口から size バイトまでを返す."""
        out = bytes(self.buf[:size])
        del self.buf[:size]
        return out


def body_of(n: int) -> bytes:
    """CSV らしい中身（n バイト）."""
    line = b"CSI,123,020000000001,5,-55,-96,11,0,1,0,0,0,0,0,htltf,128,00ff*12345678\n"
    return (line * (n // len(line) + 1))[:n]


def test_list_files_skips_csi_lines() -> None:
    """一覧は # sd file 行だけを拾う（CSI 行は飛ばす）."""
    port = FakeReceiver({"S0002.csv": b"ab", "S0001.csv": b"abc"})
    files = sf.list_files(port)
    assert [(f.name, f.size) for f in files] == [("S0001.csv", 3), ("S0002.csv", 2)]


def test_fetch_whole_file(tmp_path: Path) -> None:
    """塊ごとに CRC を確かめて、元と同じ中身を取り込む."""
    body = body_of(3 * CHUNK + 100)
    port = FakeReceiver({"S0003.csv": body})
    n = sf.fetch(port, "S0003.csv", tmp_path / "x")
    assert n == len(body) and (tmp_path / "x").read_bytes() == body


def test_fetch_retries_from_broken_chunk(tmp_path: Path) -> None:
    """壊れた塊があれば、その位置から送り直してもらう."""
    body = body_of(3 * CHUNK + 100)
    port = FakeReceiver({"S0003.csv": body}, corrupt_once=CHUNK)
    n = sf.fetch(port, "S0003.csv", tmp_path / "x")
    assert n == len(body) and (tmp_path / "x").read_bytes() == body
    assert port.commands == ["SDGET S0003.csv 0", "K", "R", "K", "K", "K"]


def test_fetch_rejects_other_names(tmp_path: Path) -> None:
    """セッションのファイル名でなければ断る."""
    with pytest.raises(sf.FetchError):
        sf.fetch(FakeReceiver({}), "../etc", tmp_path / "x")


def test_output_path_uses_header_time(tmp_path: Path) -> None:
    """名前は # capture start の時刻とセッション番号。同名があれば断る."""
    rec = tmp_path / "rec"
    rec.write_text("# capture start=2026-10-07T23:10:00+09:00\n# sd session source=button\n",
                   encoding="utf-8")
    start = sf.start_time_from_header(rec)
    assert start == datetime(2026, 10, 7, 23, 10, tzinfo=timezone(timedelta(hours=9)))
    path = sf.output_path(tmp_path, "sd", "S0007.csv", start, datetime(2026, 1, 1))
    assert path.name.endswith("-sd-S0007.csv") and path.name.startswith("2026100")
    path.write_text("x", encoding="utf-8")
    with pytest.raises(FileExistsError):
        sf.output_path(tmp_path, "sd", "S0007.csv", start, datetime(2026, 1, 1))
    assert sf.already_imported(tmp_path, "sd", "S0007.csv") == [path]
    with pytest.raises(ValueError):
        sf.output_path(tmp_path, "bad label", "S0007.csv", None, datetime(2026, 1, 1))


def test_already_imported_distinguishes_start_time(tmp_path: Path) -> None:
    """カードを空にして番号が振り直されても、開始時刻が違えば取り込み済みとしない."""
    old_start = datetime(2026, 10, 7, 17, 45, 35, tzinfo=timezone(timedelta(hours=9)))
    new_start = datetime(2026, 10, 8, 13, 3, 9, tzinfo=timezone(timedelta(hours=9)))
    old = sf.output_path(tmp_path, "sd", "S0001.csv", old_start, datetime(2026, 1, 1))
    old.write_text("x", encoding="utf-8")
    assert sf.already_imported(tmp_path, "sd", "S0001.csv", new_start) == []
    assert sf.already_imported(tmp_path, "sd", "S0001.csv", old_start) == [old]
    # 開始時刻が分からないとき（USB 経由で中身を読む前など）は番号だけで判定する
    assert sf.already_imported(tmp_path, "sd", "S0001.csv") == [old]


def test_import_dir_imports_renumbered_session(tmp_path: Path) -> None:
    """前日の S0001 があっても、開始時刻の違う当日の S0001 は取り込む（同じものは飛ばす）."""
    out_dir = tmp_path / "raw"
    out_dir.mkdir()
    card = tmp_path / "CSI"
    card.mkdir()
    old_start = datetime(2026, 10, 7, 17, 45, 35, tzinfo=timezone(timedelta(hours=9)))
    sf.output_path(out_dir, "sd", "S0001.csv", old_start,
                   datetime(2026, 1, 1)).write_text("old", encoding="utf-8")
    (card / "S0001.csv").write_text("# capture start=2026-10-08T13:03:09+09:00\n# sd session\n",
                                    encoding="utf-8")
    args = argparse.Namespace(dir=card, out_dir=out_dir, label="sd", start=None, no_meta=True,
                              experiment=None, meta_base=None, meta_dir=None, no_compress=True,
                              level=3)
    assert sf.import_dir(args, datetime(2026, 10, 8, 15, 0)) == 0
    assert len(list(out_dir.glob("*-sd-S0001.csv"))) == 2
    assert sf.import_dir(args, datetime(2026, 10, 8, 15, 0)) == 0  # 2 回目は飛ばす
    assert len(list(out_dir.glob("*-sd-S0001.csv"))) == 2


def test_import_dir_compresses_by_default(tmp_path: Path) -> None:
    """既定では .csv.zst だけを残す（展開すると元と同じ）。2 回目は .csv.zst を見て飛ばす."""
    import zstandard

    card = tmp_path / "CSI"
    card.mkdir()
    body = b"# capture start=2026-10-10T09:05:24+09:00\n# sd session source=button\nCSI,1*0\n"
    (card / "S0001.csv").write_bytes(body)
    out = tmp_path / "raw"
    argv = ["--out-dir", str(out), "--no-meta", "--level", "3", "import-dir", str(card)]
    assert sf.main(argv) == 0
    (zst,) = out.glob("*-sd-S0001.csv.zst")
    assert list(out.glob("*.csv")) == [] and list(out.glob("*.tmp")) == []
    assert zstandard.ZstdDecompressor().decompressobj().decompress(zst.read_bytes()) == body
    assert sf.main(argv) == 0
    assert list(out.iterdir()) == [zst]


def test_compress_failure_keeps_csv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                    capsys: pytest.CaptureFixture[str]) -> None:
    """圧縮に失敗しても取り込みは成功のまま、.csv を残して警告する."""
    def broken(path: Path, level: int) -> Path:
        raise ValueError("一致しません")

    monkeypatch.setattr(sf.rawzst, "compress_verified", broken)
    csv = tmp_path / "x-sd-S0001.csv"
    csv.write_bytes(b"CSI,1*0\n")
    assert sf.compress_imported(csv, 3) == csv
    assert csv.exists()
    assert "圧縮できませんでした" in capsys.readouterr().err


def test_bad_level_is_rejected(tmp_path: Path) -> None:
    """--level が範囲の外なら何もせずに終了コード 1."""
    card = tmp_path / "CSI"
    card.mkdir()
    (card / "S0001.csv").write_bytes(b"CSI,1*0\n")
    rc = sf.main(["--out-dir", str(tmp_path / "raw"), "--level", "0", "import-dir", str(card)])
    assert rc == 1 and not (tmp_path / "raw").exists()


def test_start_time_missing_is_none(tmp_path: Path) -> None:
    """時刻を教えていなかったセッションは None."""
    rec = tmp_path / "rec"
    rec.write_text("# sd session source=button time_known=0\nCSI,1*0\n", encoding="utf-8")
    assert sf.start_time_from_header(rec) is None


def test_wifi_command_hex_and_bounds() -> None:
    """名前とパスワードは 16 進で送る（空白・記号も壊れない）。長さの外は断る."""
    cmd = sf.wifi_command("my home 2G", "p@ss word!", 32400)
    _, ssid_hex, pass_hex, tz = cmd.split(" ")
    assert bytes.fromhex(ssid_hex) == b"my home 2G"
    assert bytes.fromhex(pass_hex) == b"p@ss word!" and tz == "32400"
    assert sf.wifi_command("open-net", "", 0).split(" ")[2] == "-"
    with pytest.raises(ValueError):
        sf.wifi_command("", "password1", 0)
    with pytest.raises(ValueError):
        sf.wifi_command("x" * 33, "password1", 0)
    with pytest.raises(ValueError):
        sf.wifi_command("net", "short", 0)
    with pytest.raises(ValueError):
        sf.wifi_command("net", "p" * 64, 0)


def test_parse_start_adds_local_timezone() -> None:
    """タイムゾーンの無い時刻は PC の地域の時刻とみなす。あればそのまま."""
    naive = sf.parse_start("2026-10-07T23:10")
    assert naive.tzinfo is not None and naive.hour == 23 and naive.minute == 10
    aware = sf.parse_start("2026-10-07T23:10:00+09:00")
    assert aware.utcoffset() == timedelta(hours=9)


def test_import_dir_with_manual_start(tmp_path: Path) -> None:
    """時刻の無いセッションに --start を与えると、先頭に行を足して名前にも使う."""
    card = tmp_path / "CSI"
    card.mkdir()
    body = b"# sd session source=button time_known=0\nCSI,1*0\n"
    (card / "S0004.csv").write_bytes(body)
    out = tmp_path / "raw"
    rc = sf.main(["--out-dir", str(out), "--no-meta", "--no-compress",
                  "--start", "2026-10-07T23:10:00+09:00", "import-dir", str(card)])
    assert rc == 0
    (path,) = out.glob("*-sd-S0004.csv")
    # 名前の日時は PC の地域の時刻（CI は UTC）なので、ここでは中身で確かめる
    text = path.read_bytes()
    assert text == b"# capture start=2026-10-07T23:10:00+09:00 source=manual\n" + body
    assert sf.start_time_from_header(path) == datetime(2026, 10, 7, 23, 10,
                                                       tzinfo=timezone(timedelta(hours=9)))


def test_manual_start_does_not_override_receiver_time(tmp_path: Path) -> None:
    """受信機の開始時刻があれば --start は使わない（中身も変えない）."""
    card = tmp_path / "CSI"
    card.mkdir()
    body = b"# capture start=2026-10-07T22:00:00+09:00\n# sd session source=button\nCSI,1*0\n"
    (card / "S0005.csv").write_bytes(body)
    out = tmp_path / "raw"
    rc = sf.main(["--out-dir", str(out), "--no-meta", "--no-compress",
                  "--start", "2026-10-07T23:10", "import-dir", str(card)])
    assert rc == 0
    (path,) = out.glob("*-sd-S0005.csv")
    assert path.read_bytes() == body


def test_manual_start_needs_single_file(tmp_path: Path) -> None:
    """--start は 1 ファイルずつ（どれの時刻か取り違えないように）."""
    card = tmp_path / "CSI"
    card.mkdir()
    for name in ("S0006.csv", "S0007.csv"):
        (card / name).write_bytes(b"CSI,1*0\n")
    rc = sf.main(["--out-dir", str(tmp_path / "raw"), "--no-meta", "--no-compress",
                  "--start", "2026-10-07T23:10", "import-dir", str(card)])
    assert rc == 1 and not (tmp_path / "raw").exists()


EVENTS = [
    "# sdevent seq=1 kind=disk_fail unix=1 up_s=2 session=S0005 session_s=8094 dur_ms=68 card_ok=1 "
    "err=1 sector=7112764 count=4",
    "# sdevent seq=2 kind=recover unix=1 up_s=2 session=S0006 session_s=0 dur_ms=87 card_ok=-1 "
    "err=1 sector=87 count=1",
]


def test_fetch_events_saves_without_clearing(tmp_path: Path) -> None:
    """出来事を読んで保存し、--clear が無ければ受信機には残す（CSI 行は飛ばす）."""
    port = FakeReceiver({}, events=EVENTS)
    path = sf.fetch_events(port, tmp_path, datetime(2026, 10, 10, 8, 0, 0), clear=False)
    assert path.name == "sdevents-20261010-080000.txt"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("# read_at=2026-10-10T08:00:00")
    assert lines[1] == "# sdevents count=2 total=2"
    assert lines[2:] == EVENTS
    assert port.commands == ["SDEVENTS"]
    assert port.events == EVENTS


def test_fetch_events_clears_after_saving(tmp_path: Path) -> None:
    """--clear は保存した後で SDEVENTS CLEAR を送る."""
    port = FakeReceiver({}, events=EVENTS)
    path = sf.fetch_events(port, tmp_path, datetime(2026, 10, 10, 8, 0, 0), clear=True)
    assert path.exists()
    assert port.commands == ["SDEVENTS", "SDEVENTS CLEAR"]
    assert port.events == []


def test_fetch_events_empty(tmp_path: Path) -> None:
    """出来事が 0 件でも見出しだけのファイルを書く."""
    port = FakeReceiver({})
    path = sf.fetch_events(port, tmp_path, datetime(2026, 10, 10, 8, 0, 0), clear=False)
    assert path.read_text(encoding="utf-8").splitlines()[1:] == ["# sdevents count=0 total=0"]


def test_save_events_rejects_missing_head(tmp_path: Path) -> None:
    """見出しの無い行は保存しない."""
    with pytest.raises(ValueError):
        sf.save_events(EVENTS, tmp_path, datetime(2026, 10, 10))


class ShortEventsReceiver(FakeReceiver):
    """出来事の件数を 1 件多く答える（行が途中で失われた受信機の代わり）."""

    def write(self, data: bytes) -> int:
        """見出しの件数だけを偽る."""
        n = super().write(data)
        self.buf = self.buf.replace(b"count=1 ", b"count=2 ")
        return n


def test_read_events_times_out_when_lines_missing() -> None:
    """件数より行が少なければ時間切れにする（偽の時計で待たない）."""
    port = ShortEventsReceiver({}, events=EVENTS[:1])
    ticks = iter(range(100_000))
    with pytest.raises(sf.FetchError):
        sf.read_events(port, clock=lambda: float(next(ticks)))


def test_remove_file_freed_matches() -> None:
    """消した分だけ空きが増えれば警告しない."""
    port = FakeReceiver({"S0003.csv": b"x" * (5 * 2**20)})
    assert sf.remove_file(port, "S0003.csv") is None
    assert port.commands == ["SDRM S0003.csv"]
    assert "S0003.csv" not in port.files


def test_remove_file_warns_when_space_not_freed() -> None:
    """空きが増えなければ警告の文を返す（消すこと自体は成功）."""
    port = FakeReceiver({"S0003.csv": b"x" * (5 * 2**20)})
    port.free_ok = False
    warning = sf.remove_file(port, "S0003.csv")
    assert warning is not None
    assert "0 MiB しか増えていません" in warning and "5 MiB" in warning


def test_remove_file_small_file_no_warning() -> None:
    """1 MiB に満たないファイルは切り捨てで 0 になるので警告しない."""
    port = FakeReceiver({"S0003.csv": b"x" * 1000})
    port.free_ok = False
    assert sf.remove_file(port, "S0003.csv") is None


def test_check_freed_old_receiver() -> None:
    """古い受信機（name だけの応答）は判定しない."""
    m = sf.RM_ACK.search("# ack cmd=SDRM name=S0003.csv")
    assert m is not None
    assert sf.check_freed(m) is None


def test_open_port_flushes_partial_command_line(tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """開いた直後に改行を送り、その後で受信の溜まりを捨てる（最初の命令にゴミが付いて断られた件）."""
    calls: list[str] = []

    class FakeSerial:
        def __init__(self, port: str, baud: int, timeout: float) -> None:
            calls.append("open")

        def write(self, data: bytes) -> int:
            calls.append(f"write:{data!r}")
            return len(data)

        def reset_input_buffer(self) -> None:
            calls.append("reset")

    fake = types.SimpleNamespace(Serial=FakeSerial, SerialException=OSError)
    monkeypatch.setitem(sys.modules, "serial", fake)
    monkeypatch.setattr(sf.time, "sleep", lambda sec: None)
    port = tmp_path / "ttyACM0"
    port.touch()
    sf.open_port(str(port), 921600)
    assert calls == ["open", "write:b'\\n'", "reset"]


def test_open_port_missing_port(tmp_path: Path) -> None:
    """ポートが無ければ FetchError."""
    with pytest.raises(sf.FetchError):
        sf.open_port(str(tmp_path / "nope"), 921600)
