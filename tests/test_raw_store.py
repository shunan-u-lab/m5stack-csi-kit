"""記録の圧縮（.csv.zst）と Parquet の写し（io・scripts/raw_store.py）のテスト."""

from __future__ import annotations

import importlib.util
import os
import sys
import zlib
from pathlib import Path
from types import ModuleType

import numpy as np
import pytest

from csikit import io, meta

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "raw_store.py"
OLD_SEC = 3600  # 記録中とみなされないよう、ファイルの更新時刻をこれだけ戻す [sec]


def load_script() -> ModuleType:
    """scripts/ はパッケージではないので、ファイルから読み込む."""
    spec = importlib.util.spec_from_file_location("raw_store", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["raw_store"] = module
    spec.loader.exec_module(module)
    return module


rs = load_script()


def csi_line(rx_us: int, seq: int, rng: np.random.Generator, invalid_first: bool) -> str:
    """CRC つきの format=2 の CSI 行（LLTF + HT-LTF の 256 バイト・値は乱数）."""
    raw = rng.integers(-128, 128, size=256).astype(np.int8).tobytes().hex()
    fwi = 1 if invalid_first else 0
    body = f"CSI,{rx_us},{seq},-52,-96,11,0,1,0,0,0,0,{fwi},256,{raw}"
    return f"{body}*{zlib.crc32(body.encode()):08x}"


def write_record(path: Path, n: int = 200, seed: int = 0) -> Path:
    """模擬の記録を書き、更新時刻を過去に戻す."""
    rng = np.random.default_rng(seed)
    lines = ["# capture start=2026-10-07T00:00:00", "# CSI receiver format=2"]
    lines += [csi_line(10_000 * i, 1000 + i, rng, invalid_first=i % 7 == 0) for i in range(n)]
    path.write_text("\n".join(lines) + "\n", encoding="ascii")
    old = path.stat().st_mtime - OLD_SEC
    os.utime(path, (old, old))
    return path


@pytest.mark.parametrize(("name", "want"), [
    ("20261007-001359-exp11-sleep-01.csv", "20261007-001359-exp11-sleep-01"),
    ("20261007-001359-exp11-sleep-01.csv.zst", "20261007-001359-exp11-sleep-01"),
    ("20261007-001359-exp11-sleep-01.htltf.parquet", "20261007-001359-exp11-sleep-01"),
    ("x.lltf.parquet", "x"),
    ("other.npz", "other"),
])
def test_recording_name(name: str, want: str) -> None:
    """拡張子の違いを除いて同じ記録名になる."""
    assert io.recording_name(Path("data/raw") / name) == want


def test_compress_is_lossless_and_readable(tmp_path: Path) -> None:
    """圧縮しても読み込みの結果は同じ。元は残る."""
    csv = write_record(tmp_path / "20261007-000000-t.csv")
    msg = rs.compress_one(csv, level=3, delete_original=False)
    zst = tmp_path / "20261007-000000-t.csv.zst"
    assert "圧縮・一致" in msg and zst.exists() and csv.exists()
    a, b = io.load_rx_csv(csv), io.load_rx_csv(zst)
    assert np.array_equal(a.csi, b.csi, equal_nan=True)
    assert np.array_equal(a.seq, b.seq)
    assert not list(tmp_path.glob("*.tmp"))


def test_compress_delete_original_and_existing(tmp_path: Path) -> None:
    """--delete-original は一致の後に元を消す。既にある .zst は確かめるだけ."""
    csv = write_record(tmp_path / "r.csv")
    rs.compress_one(csv, level=3, delete_original=False)
    assert "既にあり一致" in rs.compress_one(csv, level=3, delete_original=True)
    assert not csv.exists()


def test_compress_refuses_mismatched_existing(tmp_path: Path) -> None:
    """既にある .zst の中身が違えば止め、元を消さない."""
    csv = write_record(tmp_path / "r.csv", seed=0)
    other = write_record(tmp_path / "o.csv", seed=1)
    rs.compress_one(other, level=3, delete_original=False)
    (tmp_path / "o.csv.zst").rename(tmp_path / "r.csv.zst")
    with pytest.raises(ValueError, match="一致しません"):
        rs.compress_one(csv, level=3, delete_original=True)
    assert csv.exists()


def test_compress_skips_recent_file(tmp_path: Path) -> None:
    """書き換えたばかりの CSV は記録中とみなして飛ばす."""
    csv = write_record(tmp_path / "r.csv")
    with pytest.raises(ValueError, match="記録中"):
        rs.compress_one(csv, level=3, delete_original=True,
                        now=lambda: csv.stat().st_mtime + 1.0)
    assert not (tmp_path / "r.csv.zst").exists()


def test_parquet_round_trip_keeps_nan(tmp_path: Path) -> None:
    """Parquet の写しから読み戻すと、無効なサブキャリアの NaN も含めて同じ."""
    csv = write_record(tmp_path / "20261007-000000-t.csv")
    # first_word_invalid の行は LLTF の先頭のサブキャリアが NaN になる（HT-LTF には及ばない）
    msg = rs.parquet_one(csv, tmp_path / "pq", "lltf", None, force=False)
    out = tmp_path / "pq" / "20261007-000000-t.lltf.parquet"
    assert out.exists() and "200 行" in msg
    a, b = io.load_rx_csv(csv, ltf="lltf"), io.load_rx(out, ltf="lltf")
    assert np.isnan(a.csi).any()
    assert np.array_equal(a.csi, b.csi, equal_nan=True)
    assert np.array_equal(a.time_sec, b.time_sec)
    assert "飛ばす" in rs.parquet_one(csv, tmp_path / "pq", "lltf", None, force=False)


def test_load_rx_rejects_other_ltf(tmp_path: Path) -> None:
    """写しと違う区間を求めたら ValueError."""
    csv = write_record(tmp_path / "t.csv")
    rs.parquet_one(csv, tmp_path, "htltf", None, force=False)
    with pytest.raises(ValueError, match="区間"):
        io.load_rx(tmp_path / "t.htltf.parquet", ltf="lltf")


def test_check_dirs_pairs_zst_with_meta(tmp_path: Path) -> None:
    """.csv.zst だけになっても、メタデータと対応がつく."""
    raw, meta_dir = tmp_path / "raw", tmp_path / "meta"
    raw.mkdir()
    meta_dir.mkdir()
    csv = write_record(raw / "20261007-000000-t.csv")
    meta.write_draft(csv, meta_dir)
    rs.compress_one(csv, level=3, delete_original=True)
    report = meta.check_dirs(raw, meta_dir)
    assert report.checked == 1 and not report.missing and not report.orphans
    assert io.find_raw(raw, "20261007-000000-t").name.endswith(".csv.zst")
    with pytest.raises(FileNotFoundError):
        io.find_raw(raw, "none")


def test_main_reports_failures(tmp_path: Path) -> None:
    """無いファイルは失敗として数え、終了コード 1."""
    assert rs.main(["compress", str(tmp_path / "none.csv")]) == 1
    assert rs.main(["compress", "--level", "0", str(tmp_path / "x.csv")]) == 1


def test_peer_tx_power_per_row_follows_stats(tmp_path: Path) -> None:
    """各 CSI 行に直前の # stats の送信機の出力を割り当てる.

    負の値と、最初の # stats より前の行は NaN。
    """
    csv = write_record(tmp_path / "p.csv", n=30)
    lines = csv.read_text(encoding="ascii").splitlines()
    head, body = lines[:2], lines[2:]
    stats = "# stats rate_hz=99.0 ctrl_bad=0 peer_tx_power_dbm="
    out = (head + body[:5] + [stats + "19.50"] + body[5:15] + [stats + "-1.00"]
           + body[15:20] + [stats + "6.50"] + body[20:])
    csv.write_text("\n".join(out) + "\n", encoding="ascii")
    pw = io.peer_tx_power_per_row(csv)
    assert len(pw) == len(io.load_rx_csv(csv).seq) == 30
    assert np.isnan(pw[:5]).all() and (pw[5:15] == 19.5).all()
    assert np.isnan(pw[15:20]).all() and (pw[20:] == 6.5).all()
