"""記録ごとのメタデータ（csikit.meta）のテスト."""

from __future__ import annotations

import copy
import sys
import tracemalloc
import zlib
from pathlib import Path

import pytest

from csikit import io, meta

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

REPO_META_DIR = Path(__file__).resolve().parent.parent / "examples" / "meta"
RAW_HEX = "00" * 256  # len=256 の CSI（中身は問わない）
PACKET_INTERVAL_US = 10_000  # 100 Hz
FIRST_RX_US = 5_000_000
EXP01_STATIC = REPO_META_DIR / "20260101-120000-static.toml"


def csi_line(i: int) -> str:
    """パケット i の CSI 行（format=2・CRC つき）."""
    body = f"CSI,{FIRST_RX_US + i * PACKET_INTERVAL_US},{i},-60,-96,11,0,1,0,0,0,0,0,256,{RAW_HEX}"
    return f"{body}*{zlib.crc32(body.encode()):08x}"


def write_capture(path: Path, n_packets: int = 300,
                  marks: dict[int, str] | None = None) -> Path:
    """live_csi --record と同じ形の記録を書く（marks: パケット番号 → 印のラベル）."""
    lines = ["# capture start=2026-09-23T15:00:00 port=/dev/ttyUSB0 baud=921600 "
             "duration_sec=live reset=True tool=live_csi",
             "# CSI receiver format=2",
             "# mac=020000000002 channel=11 (want 11) tx_mac=020000000001 baud=921600"]
    for i in range(n_packets):
        lines.append(csi_line(i))
        if marks and i in marks:
            lines.append(f"# mark label={marks[i]} seq={i} "
                         f"rx_us={FIRST_RX_US + i * PACKET_INTERVAL_US} wall=x")
    lines.append(f"# summary ok={n_packets} bad_crc=2 bad_ratio=0.0010 bad_format=0 other=1 "
                 f"span_sec=2.99 rate_hz=100.0 est_missing=0 seq_unknown=0 missing_seq=0")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_dumps_roundtrip() -> None:
    """書き出した TOML を読み戻すと同じ内容になる（入れ子・表の配列・記号・日本語）."""
    data = {
        "schema": 1, "name": 'a "quoted"\\ path\nnext\ttab', "ratio": 0.5, "flag": False,
        "tags": ["x", "日本語"], "empty": [],
        "table": {"n": 3, "sub": {"deep": True}, "nodes": [{"name": "S"}, {"name": "R"}]},
        "items": [{"k": 1}, {"k": 2, "note": "two"}],
        "odd key": "v",
    }
    text = meta.dumps_meta(data, header=["見出し"])
    assert text.startswith("# 見出し\n")
    assert tomllib.loads(text) == data


def test_dumps_rejects_unsupported_values() -> None:
    """None と NaN は書かない."""
    with pytest.raises(ValueError):
        meta.dumps_meta({"x": None})
    with pytest.raises(ValueError):
        meta.dumps_meta({"x": float("nan")})


def test_committed_meta_files_are_valid() -> None:
    """リポジトリの examples/meta の全ファイルが読め、形の問題がなく、ファイル名と一致する."""
    files = sorted(REPO_META_DIR.glob("*.toml"))
    assert len(files) >= 3  # 雛形 + 記入例 2 本
    for path in files:
        expected = None if path.name == meta.TEMPLATE_NAME else path.stem
        assert meta.validate_meta(meta.load_meta(path), expected) == [], path.name


def test_validate_reports_structural_problems() -> None:
    """代表的な誤りをそれぞれ見つける."""
    base = meta.load_meta(EXP01_STATIC)
    assert meta.validate_meta(base, "20260101-120000-static") == []
    cases = {
        "schema": lambda m: m.update(schema=2),
        "recording": lambda m: m.update(recording="other"),
        "time.start": lambda m: m["time"].update(start="2026-09-23T09:05:34"),  # タイムゾーンなし
        "devices": lambda m: m.update(devices=[m["devices"][0]]),  # 受信機がない
        "devices[0].mac": lambda m: m["devices"][0].update(mac="02000000000a"),
        "radio.channel": lambda m: m["radio"].update(channel=40),
        "segments[0].end_sec": lambda m: m["segments"][0].update(end_sec=99),
        "status.usable": lambda m: m["status"].update(usable="yes"),
        "[placement]": lambda m: m.pop("placement"),
    }
    for key, mutate in cases.items():
        broken = copy.deepcopy(base)
        mutate(broken)
        problems = meta.validate_meta(broken, "20260101-120000-static")
        assert any(p.startswith(key) for p in problems), (key, problems)


def test_placeholders_are_allowed_but_todo_is_listed() -> None:
    """"unknown" と "TODO" は形の問題にしないが、TODO は未記入として数える."""
    m = meta.load_meta(EXP01_STATIC)
    m["placement"]["room"] = meta.TODO
    m["placement"]["tx_rx_distance_m"] = meta.UNKNOWN
    assert meta.validate_meta(m) == []
    assert meta.find_todos(m) == ["placement.room"]


def test_draft_from_capture_fills_automatic_fields(tmp_path: Path) -> None:
    """記録から時刻・MAC・チャンネル・品質・印を埋め、印で区間を分ける."""
    csv = write_capture(tmp_path / "20260923-150000-exp04-run1.csv",
                        marks={50: "baseline", 100: "walk", 200: "static", 250: "mark"})
    draft = meta.draft_from_capture(csv, experiment="exp04")
    assert draft["recording"] == "20260923-150000-exp04-run1"
    assert draft["label"] == "exp04-run1"
    assert draft["time"]["start"].startswith("2026-09-23T15:00:00")
    assert draft["time"]["duration_sec"] == pytest.approx(2.99)
    macs = {d["role"]: d["mac"] for d in draft["devices"]}
    assert macs == {"tx": "02:00:00:00:00:01", "rx": "02:00:00:00:00:02"}
    assert draft["radio"]["channel"] == 11
    assert draft["quality"]["bad_crc"] == 2 and draft["quality"]["missing_seq"] == 0
    assert [(s["start_sec"], s["end_sec"], s["activity"]) for s in draft["segments"]] == [
        (0.0, 1.0, meta.TODO), (1.0, 2.0, "walk"), (2.0, 2.99, "static")]
    assert [e["t_sec"] for e in draft["events"]] == [0.5, 2.5]
    assert draft["events"][0]["confirmed"] is True  # baseline
    # 形の問題はなく、人が書く項目は TODO として並ぶ。書き出して読み戻しても同じ
    assert meta.validate_meta(draft, csv.stem) == []
    todos = meta.find_todos(draft)
    assert "placement.room" in todos and "purpose" in todos and "events[1].note" in todos
    assert tomllib.loads(meta.dumps_meta(draft)) == draft


def test_draft_ignores_tx_list_when_reading_rx_mac(tmp_path: Path) -> None:
    """format=3 の送信機の一覧（# tx id=N mac=…）を受信機の MAC と取り違えない."""
    csv = write_capture(tmp_path / "20261006-120000-exp08-run1.csv")
    lines = csv.read_text(encoding="utf-8").splitlines()
    rx_header = next(i for i, line in enumerate(lines) if line.startswith("# mac="))
    lines[rx_header + 1:rx_header + 1] = [
        "# tx id=0 mac=020000000001 name=ESP32-DevKit primary=1",
        "# tx id=1 mac=02000000000c name=PicoW"]
    csv.write_text("\n".join(lines) + "\n", encoding="utf-8")
    draft = meta.draft_from_capture(csv, experiment="exp08")
    macs = {d["role"]: d["mac"] for d in draft["devices"]}
    assert macs == {"tx": "02:00:00:00:00:01", "rx": "02:00:00:00:00:02"}


def test_draft_fills_firmware_rev_from_capture(tmp_path: Path) -> None:
    """受信機の版は起動の行・WHO の応答から、送信機の版は # capture tx_fw= の行から入る."""
    csv = write_capture(tmp_path / "20261029-120000-walk.csv")
    draft = meta.draft_from_capture(csv)
    assert {d["role"]: d["firmware_rev"] for d in draft["devices"]} == {
        "tx": meta.UNKNOWN, "rx": meta.UNKNOWN}  # 版の無い古い記録は unknown のまま

    lines = csv.read_text(encoding="utf-8").splitlines()
    lines[1:1] = [meta.tx_fw_line("1028-3b8f078") or ""]
    lines = [line + " fw=1028-aaaaaaa" if line.startswith("# mac=") else line for line in lines]
    lines.insert(3, "# tx id=0 mac=020000000001 name=x fw=9999-bbbbbbb")  # 送信機の一覧は見ない
    csv.write_text("\n".join(lines) + "\n", encoding="utf-8")
    info = meta.read_capture_info(csv)
    assert (info.rx_fw, info.tx_fw) == ("1028-aaaaaaa", "1028-3b8f078")
    assert info.rx_mac == "02:00:00:00:00:02"  # 版の行を受信機の MAC と取り違えない
    draft = meta.draft_from_capture(csv)
    assert {d["role"]: d["firmware_rev"] for d in draft["devices"]} == {
        "tx": "1028-3b8f078", "rx": "1028-aaaaaaa"}
    assert meta.validate_meta(draft, csv.stem) == []


def test_draft_keeps_base_firmware_rev_when_capture_has_none(tmp_path: Path) -> None:
    """記録に版が無ければ前回の値を残し、あれば記録の値で上書きする（MAC と同じ）."""
    base = meta.load_meta(EXP01_STATIC)
    for dev in base["devices"]:
        dev["firmware_rev"] = "1001-0000000"
    csv = write_capture(tmp_path / "20261029-120000-walk.csv")
    lines = csv.read_text(encoding="utf-8").splitlines()
    lines[1:1] = [meta.tx_fw_line("1028-3b8f078") or ""]
    csv.write_text("\n".join(lines) + "\n", encoding="utf-8")
    draft = meta.draft_from_capture(csv, base=base)
    assert {d["role"]: d["firmware_rev"] for d in draft["devices"]} == {
        "tx": "1028-3b8f078", "rx": "1001-0000000"}


def test_tx_fw_line_rejects_non_version_text() -> None:
    """記録を壊す文字（空白・改行）や unknown は行にしない."""
    assert meta.tx_fw_line("1028-3b8f078") == "# capture tx_fw=1028-3b8f078"
    for bad in ("", "unknown", "1028 x", "a\nb"):
        assert meta.tx_fw_line(bad) is None, bad


def test_draft_without_marks_uses_label_as_activity(tmp_path: Path) -> None:
    """印がなければ全体を 1 区間とし、ラベルが動作名ならそれを使う."""
    csv = write_capture(tmp_path / "20260923-150000-walk.csv")
    draft = meta.draft_from_capture(csv)
    assert draft["segments"] == [{"start_sec": 0.0, "end_sec": 2.99, "activity": "walk",
                                  "where": meta.TODO}]
    assert "events" not in draft


def test_draft_copies_base_and_flags_changed_board(tmp_path: Path) -> None:
    """前回のメタデータから配置などを写し、MAC が変わった M5Stack Basic は名前を確かめ直させる."""
    base = meta.load_meta(EXP01_STATIC)
    base["devices"][1]["mac"] = "aa:bb:cc:dd:ee:ff"  # 前回は別の受信機だった
    csv = write_capture(tmp_path / "20260923-150000-walk.csv")
    draft = meta.draft_from_capture(csv, base=base)
    assert draft["placement"] == base["placement"]
    assert draft["subjects"] == base["subjects"]
    tx, rx = draft["devices"]
    assert (tx["name"], rx["name"]) == ("T1", meta.TODO)
    assert rx["mac"] == "02:00:00:00:00:02"


def test_write_draft_never_overwrites(tmp_path: Path) -> None:
    """下書きを書き、同名があれば上書きしない."""
    csv = write_capture(tmp_path / "20260923-150000-walk.csv")
    out = meta.write_draft(csv, tmp_path / "meta", "exp04", base_path=EXP01_STATIC)
    assert out.name == "20260923-150000-walk.toml"
    text = out.read_text(encoding="utf-8")
    assert "20260101-120000-static.toml から写した" in text
    assert meta.validate_meta(meta.load_meta(out), out.stem) == []
    with pytest.raises(FileExistsError):
        meta.write_draft(csv, tmp_path / "meta")


TX_S = "02000000000a"  # 主の送信機（見出しの tx id=0）
TX_P = "02000000000c"  # 2 台目
RAW_HEX_LTF = "00" * 128  # format=3 の区間 1 つ分（中身は問わない）
WRAP_START_RX_US = io.RX_TIMESTAMP_MODULUS - 25_000  # 3 パケット目で 32bit の一周をまたぐ


def rx_line(rx_us: int, seq: int, rssi_dbm: int, tx: str | None = None) -> str:
    """CRC つきの CSI 行（tx を渡すと format=3 の htltf、省略で format=2）."""
    rx_us %= io.RX_TIMESTAMP_MODULUS
    if tx is None:
        body = f"CSI,{rx_us},{seq},{rssi_dbm},-96,11,0,1,0,0,0,0,0,256,{RAW_HEX}"
    else:
        body = (f"CSI,{rx_us},{tx},{seq},{rssi_dbm},-96,11,0,1,0,0,0,0,0,htltf,128,"
                f"{RAW_HEX_LTF}")
    return f"{body}*{zlib.crc32(body.encode()):08x}"


def test_capture_info_matches_load_rx_csv_across_wrap(tmp_path: Path) -> None:
    """1 行ずつの集計が、配列に読み込んだ結果（io.load_rx_csv）と同じ長さ・RSSI になる."""
    csv = tmp_path / "20261009-100000-sd-S0001.csv"
    lines = ["# CSI receiver format=2"]
    lines += [rx_line(WRAP_START_RX_US + i * PACKET_INTERVAL_US, i, -50 - i % 7)
              for i in range(50)]
    lines.append(f"# mark label=walk seq=10 rx_us={WRAP_START_RX_US + 10 * PACKET_INTERVAL_US}")
    csv.write_text("\n".join(lines) + "\n", encoding="utf-8")
    info = meta.read_capture_info(csv)
    cap = io.load_rx_csv(csv)
    assert info.duration_sec == round(float(cap.time_sec[-1]), meta.TIME_DECIMALS) == 0.49
    assert info.rssi_mean_dbm == round(float(cap.rssi_dbm.mean()), meta.RSSI_DECIMALS)
    assert info.marks == [("walk", 0.1)]


def test_capture_info_two_transmitters_uses_primary(tmp_path: Path) -> None:
    """送信機 2 台の記録は主の送信機の行で集計し、主が分からなければ選ぶよう求める."""
    header = ["# CSI receiver format=3", f"# tx id=0 mac={TX_S} primary=1", f"# tx id=1 mac={TX_P}"]
    body = [rx_line(FIRST_RX_US - 3000, 100, -70, TX_P)]  # 2 台目が先に届く
    for i in range(20):
        body.append(rx_line(FIRST_RX_US + i * PACKET_INTERVAL_US, i, -50, TX_S))
        body.append(rx_line(FIRST_RX_US + i * PACKET_INTERVAL_US + 3000, 101 + i, -70, TX_P))
    body.append(f"# mark label=walk seq=5 rx_us={FIRST_RX_US + 5 * PACKET_INTERVAL_US}")
    csv = tmp_path / "20261009-100000-exp08-two.csv"
    csv.write_text("\n".join(header + body) + "\n", encoding="utf-8")
    info = meta.read_capture_info(csv)
    assert (info.duration_sec, info.rssi_mean_dbm, info.marks) == (0.19, -50.0, [("walk", 0.05)])
    no_header = tmp_path / "20261009-100000-exp08-noheader.csv"
    no_header.write_text("\n".join(body) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="tx で選んで"):
        meta.read_capture_info(no_header)


def test_capture_info_marks_in_long_recording(tmp_path: Path) -> None:
    """約 71.6 分（32bit の一周）を超えた後の印も、先頭からの正しい時刻になる."""
    gap_us = 20 * 60 * 1_000_000  # パケットの間隔 20 分（一周の半分より短い）
    lines = ["# CSI receiver format=2"]
    for i in range(10):  # 3 時間・受信時刻は 2 回一周する
        lines.append(rx_line(FIRST_RX_US + i * gap_us, i, -55))
        if i == 8:
            mark_us = (FIRST_RX_US + i * gap_us + 5_000_000) % io.RX_TIMESTAMP_MODULUS
            lines.append(f"# mark label=walk seq=8 rx_us={mark_us}")
    csv = tmp_path / "20261008-152851-sd-S0002.csv"
    csv.write_text("\n".join(lines) + "\n", encoding="utf-8")
    info = meta.read_capture_info(csv)
    assert info.duration_sec == 9 * 1200.0
    assert info.marks == [("walk", 8 * 1200.0 + 5.0)]  # 以前は 1 周分ずれて 1015.03 になっていた


def test_capture_info_streams_without_loading_csi(tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """CSI を配列に読み込まない（2.2 GB の記録で OOM になった）。メモリは記録の長さによらない."""
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("CSI を配列に読み込んだ")

    monkeypatch.setattr(io, "load_rx_csv", forbidden)
    monkeypatch.setattr(io, "lines_to_capture", forbidden)
    peaks_bytes = []
    for n_packets in (500, 5000):
        csv = write_capture(tmp_path / f"20261009-100000-n{n_packets}.csv", n_packets=n_packets)
        tracemalloc.start()
        info = meta.read_capture_info(csv)
        peaks_bytes.append(tracemalloc.get_traced_memory()[1])
        tracemalloc.stop()
        assert info.duration_sec == round((n_packets - 1) * PACKET_INTERVAL_US / 1e6,
                                          meta.TIME_DECIMALS)
    assert peaks_bytes[1] < 2 * peaks_bytes[0], peaks_bytes  # 10 倍の記録でも 2 倍未満


def test_capture_info_reports_broken_line_number(tmp_path: Path) -> None:
    """CRC の合わない行は、行番号つきで知らせる."""
    csv = write_capture(tmp_path / "20261009-100000-broken.csv", n_packets=10)
    lines = csv.read_text(encoding="utf-8").splitlines()
    lines[5] = lines[5][:-1] + ("0" if lines[5][-1] != "0" else "1")  # CRC を 1 文字変える
    csv.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"broken\.csv:6:"):
        meta.read_capture_info(csv)
    with pytest.raises(FileNotFoundError):
        meta.read_capture_info(tmp_path / "missing.csv")


def test_check_dirs_reports_missing_errors_todos_orphans(tmp_path: Path) -> None:
    """欠け・形の問題・未記入・記録のないメタデータを分けて報告する."""
    raw, meta_dir = tmp_path / "raw", tmp_path / "meta"
    raw.mkdir()
    meta_dir.mkdir()
    for name in ("20260923-150000-walk", "20260923-150100-static", "20260923-150200-walk"):
        write_capture(raw / f"{name}.csv")
    meta.write_draft(raw / "20260923-150000-walk.csv", meta_dir)  # TODO が残る
    # 読めない
    (meta_dir / "20260923-150100-static.toml").write_text("schema = [", encoding="utf-8")
    # 記録なし
    (meta_dir / "20260922-000000-old.toml").write_text(
        EXP01_STATIC.read_text(encoding="utf-8"), encoding="utf-8")
    (meta_dir / meta.TEMPLATE_NAME).write_text("x", encoding="utf-8")  # 雛形は対象外
    report = meta.check_dirs(raw, meta_dir)
    assert report.checked == 3
    assert report.missing == ["20260923-150200-walk"]
    assert set(report.errors) == {"20260923-150100-static", "20260922-000000-old"}  # 名前違い
    assert list(report.todos) == ["20260923-150000-walk"]
    assert report.orphans == ["20260922-000000-old"]
    assert not report.ok
