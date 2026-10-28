"""scripts/mask_macs.py・csikit.anonymize: 実機の MAC をダミーに置き換えた公開用の写し."""

from __future__ import annotations

import importlib.util
import sys
import zlib
from pathlib import Path
from types import ModuleType

import pytest

from csikit import anonymize, io

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "mask_macs.py"
REAL_TX = "a4cf12345678"  # 実機に見立てた MAC（テスト用。ダミーの 02:00… ではない）
REAL_RX = "a4cf1234abcd"
HEX_HTLTF = "0304" * 64  # 64 サブキャリア（3 + 4j）
NAME = "20261028-114819-static"


def load_script() -> ModuleType:
    """scripts/ はパッケージではないので、ファイルから読み込む."""
    spec = importlib.util.spec_from_file_location("mask_macs", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["mask_macs"] = module
    spec.loader.exec_module(module)
    return module


def csi_line(seq: int, tx: str = REAL_TX) -> str:
    """CRC つきの CSI 行（format=3・HT-LTF）."""
    body = f"CSI,{seq * 10_000},{tx},{seq},-50,-96,11,0,1,0,0,0,0,0,htltf,128,{HEX_HTLTF}"
    return f"{body}*{zlib.crc32(body.encode()):08x}"


def write_recording(tmp: Path, extra: list[str] | None = None) -> tuple[Path, Path]:
    """実機の MAC を含む記録とメタデータを作る."""
    raw, meta_dir = tmp / "raw", tmp / "meta"
    raw.mkdir()
    meta_dir.mkdir()
    lines = ["# capture start=2026-10-28T11:48:19 port=/dev/ttyACM1 baud=921600",
             f"# who role=rx mac={REAL_RX} channel=11 format=3 tx_mac={REAL_TX} relay=1 pico=0 "
             "auth=on tx_power_dbm=19.50 fw=1028-a74b527 paired=1",
             *[csi_line(i) for i in range(20)], *(extra or [])]
    csv = raw / f"{NAME}.csv"
    csv.write_text("\n".join(lines) + "\n", encoding="utf-8")
    colon = anonymize.colon
    (meta_dir / f"{NAME}.toml").write_text(
        f'[[devices]]\nrole = "tx"\nmac = "{colon(REAL_TX)}"\n\n'
        f'[[devices]]\nrole = "rx"\nmac = "{colon(REAL_RX).upper()}"\n', encoding="utf-8")
    return csv, meta_dir


def test_masks_csv_and_meta_and_keeps_rows(tmp_path: Path) -> None:
    """送信機 → …:01・受信機 → …:02 に置き換え、CRC を付け直し、行の数は変わらない."""
    csv, meta_dir = write_recording(tmp_path)
    out = tmp_path / "sample"
    assert load_script().main([str(csv), "--out-dir", str(out), "--meta-dir", str(meta_dir)]) == 0
    text = (out / f"{NAME}.csv").read_text(encoding="utf-8")
    meta = (out / f"{NAME}.toml").read_text(encoding="utf-8")
    for real in (REAL_TX, REAL_RX):
        assert real not in text.lower() and anonymize.colon(real) not in meta.lower()
    assert f"mac={anonymize.DUMMY_RX} " in text and f"tx_mac={anonymize.DUMMY_TX} " in text
    assert 'mac = "02:00:00:00:00:01"' in meta and 'mac = "02:00:00:00:00:02"' in meta
    cap = io.load_rx_csv(out / f"{NAME}.csv")  # CRC が合わなければ読めない
    assert len(cap.seq) == 20
    assert anonymize.find_leaks(text) == 0
    assert csv.read_text(encoding="utf-8").count(REAL_TX) > 0  # 元は書き換えない


def test_refuses_recordings_with_nearby_wifi(tmp_path: Path) -> None:
    """周りの Wi-Fi（beacon の bssid・ssid）を含む記録は写さない."""
    csv, meta_dir = write_recording(
        tmp_path, extra=["# beacon ch=6 bssid=112233445566 ap_ch=6 n=3 csi=0 ssid_hex=6162"])
    out = tmp_path / "sample"
    assert load_script().main([str(csv), "--out-dir", str(out), "--meta-dir", str(meta_dir)]) == 1
    assert not (out / f"{NAME}.csv").exists()


def test_refuses_broken_crc_and_overwrite_and_same_dir(tmp_path: Path,
                                                       capsys: pytest.CaptureFixture[str]) -> None:
    """CRC の合わない行・既にある出力・元と同じフォルダは断る。理由に実機の MAC を出さない."""
    script = load_script()
    csv, meta_dir = write_recording(tmp_path, extra=[csi_line(99)[:-1] + "0"])
    out = tmp_path / "sample"
    assert script.main([str(csv), "--out-dir", str(out), "--meta-dir", str(meta_dir)]) == 1
    assert "CRC" in capsys.readouterr().err
    csv.write_text("\n".join(csv.read_text(encoding="utf-8").splitlines()[:-1]) + "\n",
                   encoding="utf-8")
    assert script.main([str(csv), "--out-dir", str(out), "--meta-dir", str(meta_dir)]) == 0
    assert script.main([str(csv), "--out-dir", str(out), "--meta-dir", str(meta_dir)]) == 1
    assert script.main([str(csv), "--out-dir", str(csv.parent), "--meta-dir", str(meta_dir)]) == 1
    err = capsys.readouterr().err
    assert "上書きしません" in err and "同じフォルダ" in err
    assert REAL_TX not in err.lower() and REAL_RX not in err.lower()


def test_find_leaks_catches_unkeyed_mac() -> None:
    """キーの無い所に残った MAC も見逃さない（見逃さない側に倒す）."""
    assert anonymize.find_leaks(f"# note something {REAL_TX}") == 1
    assert anonymize.find_leaks("# who mac=020000000002 tx_mac=000000000000") == 0


def test_other_macs_get_other_dummies() -> None:
    """送信機・受信機以外の送信元（ALLCSI など）は 02:00:00:00:00:1x にする."""
    lines = [f"# who role=rx mac={REAL_RX} channel=11 format=3 tx_mac={REAL_TX} relay=1",
             csi_line(0), csi_line(1, tx="b8d61a000001")]
    mapping = anonymize.build_mapping(lines)
    assert mapping[REAL_TX] == anonymize.DUMMY_TX and mapping[REAL_RX] == anonymize.DUMMY_RX
    assert mapping["b8d61a000001"] == "020000000010"
