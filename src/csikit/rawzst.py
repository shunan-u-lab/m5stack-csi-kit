"""記録（``.csv``）を zstd で圧縮し、中身が元と同じか確かめる（ADR 0004）.

``scripts/raw_store.py compress`` と、取り込みの直後に圧縮する ``scripts/sd_fetch.py`` が使う。

失敗時の挙動: 圧縮したものを展開して元と SHA-256 が一致しなければ、書きかけを消して
``ValueError``。``.csv.zst`` が既にあれば上書きしない。元の CSV はここでは消さない。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import BinaryIO

from csikit import io

DEFAULT_LEVEL = 19  # zstd の圧縮の強さ（1 時間 120 MB → 約 26 MB・約 30 秒。3 なら約 39 MB・1 秒）
MAX_LEVEL = 22
CHUNK_BYTES = 1 << 20  # 読み書きの単位 [byte]
TMP_SUFFIX = ".tmp"


def sha256_of(stream: BinaryIO) -> str:
    """ストリームの終わりまでの SHA-256 を返す.

    Args:
        stream: 読むストリーム。

    Returns:
        16 進の SHA-256。
    """
    h = hashlib.sha256()
    while chunk := stream.read(CHUNK_BYTES):
        h.update(chunk)
    return h.hexdigest()


def zst_matches(csv_path: Path, zst_path: Path) -> bool:
    """``zst_path`` を展開した中身が ``csv_path`` とバイト単位で同じか.

    Args:
        csv_path: 元の CSV。
        zst_path: 圧縮したファイル。

    Returns:
        一致すれば True（展開できなければ False）。
    """
    import zstandard

    with csv_path.open("rb") as f:
        want = sha256_of(f)
    try:
        with zst_path.open("rb") as raw, zstandard.ZstdDecompressor().stream_reader(raw) as zs:
            got = sha256_of(zs)
    except zstandard.ZstdError:
        return False
    return got == want


def zst_path_for(csv_path: Path) -> Path:
    """CSV に対応する ``.csv.zst`` のパス.

    Args:
        csv_path: 元の CSV（``.csv``）。

    Returns:
        ``<記録名>.csv.zst``。

    Raises:
        ValueError: ``.csv`` でない場合。
    """
    if not csv_path.name.endswith(io.RAW_CSV_SUFFIX):
        raise ValueError(f".csv ではありません: {csv_path}")
    return csv_path.with_name(io.recording_name(csv_path) + io.RAW_ZST_SUFFIX)


def compress_verified(csv_path: Path, level: int = DEFAULT_LEVEL) -> Path:
    """``<記録名>.csv.zst`` を作り、展開した中身が元と一致することを確かめる.

    ``<記録名>.csv.zst.tmp`` に書いて確かめてから改名するので、途中で止まっても
    不完全な ``.csv.zst`` は残らない。元の CSV は消さない（消すかは呼び出し側が決める）。

    Args:
        csv_path: 元の CSV。
        level: zstd の圧縮の強さ（1〜``MAX_LEVEL``）。

    Returns:
        作った ``.csv.zst``。

    Raises:
        ValueError: 強さが範囲外・``.csv`` でない・圧縮した中身が一致しない場合。
        FileExistsError: ``.csv.zst`` が既にある場合。
        OSError: 読み書きできない場合。
    """
    import zstandard

    if not 1 <= level <= MAX_LEVEL:
        raise ValueError(f"圧縮の強さは 1〜{MAX_LEVEL}: {level}")
    zst = zst_path_for(csv_path)
    if zst.exists():
        raise FileExistsError(f"既にあります（上書きしません）: {zst}")
    tmp = zst.with_name(zst.name + TMP_SUFFIX)
    cctx = zstandard.ZstdCompressor(level=level, threads=-1, write_checksum=True)
    try:
        with csv_path.open("rb") as src, tmp.open("wb") as dst:
            cctx.copy_stream(src, dst, read_size=CHUNK_BYTES, write_size=CHUNK_BYTES)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if not zst_matches(csv_path, tmp):
        tmp.unlink(missing_ok=True)
        raise ValueError("圧縮したものを展開すると元と一致しません（消しました）")
    tmp.replace(zst)
    return zst
