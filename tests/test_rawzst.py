"""csikit.rawzst（記録の圧縮と一致の確認）のテスト."""

from __future__ import annotations

from pathlib import Path

import pytest

from csikit import rawzst


def test_compress_verified_round_trip(tmp_path: Path) -> None:
    """.csv.zst を作り、中身が元と一致する。元の CSV は消さない."""
    csv = tmp_path / "20261010-090524-sd-S0001.csv"
    csv.write_bytes(b"# capture start=2026-10-10T09:05:24+09:00\nCSI,1*0\n" * 100)
    zst = rawzst.compress_verified(csv, level=3)
    assert zst.name == "20261010-090524-sd-S0001.csv.zst"
    assert rawzst.zst_matches(csv, zst) and csv.exists()
    assert not zst.with_name(zst.name + rawzst.TMP_SUFFIX).exists()


def test_compress_verified_refuses_existing(tmp_path: Path) -> None:
    """.csv.zst が既にあれば上書きしない."""
    csv = tmp_path / "rec.csv"
    csv.write_bytes(b"CSI,1*0\n")
    (tmp_path / "rec.csv.zst").write_bytes(b"old")
    with pytest.raises(FileExistsError):
        rawzst.compress_verified(csv, level=3)
    assert (tmp_path / "rec.csv.zst").read_bytes() == b"old"


@pytest.mark.parametrize("level", [0, rawzst.MAX_LEVEL + 1])
def test_compress_verified_rejects_bad_level(tmp_path: Path, level: int) -> None:
    """強さが 1〜MAX_LEVEL の外なら断る."""
    csv = tmp_path / "rec.csv"
    csv.write_bytes(b"CSI,1*0\n")
    with pytest.raises(ValueError):
        rawzst.compress_verified(csv, level=level)


def test_compress_verified_cleans_up_on_mismatch(tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """展開した中身が一致しなければ書きかけを消して ValueError（.csv.zst を残さない）."""
    csv = tmp_path / "rec.csv"
    csv.write_bytes(b"CSI,1*0\n")
    monkeypatch.setattr(rawzst, "zst_matches", lambda a, b: False)
    with pytest.raises(ValueError):
        rawzst.compress_verified(csv, level=3)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["rec.csv"]
