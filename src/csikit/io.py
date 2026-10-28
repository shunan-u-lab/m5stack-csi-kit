"""CSI データの入出力雛形.

CSI はプラットフォームごとに保存形式が異なる（Intel 5300 の .dat、Nexmon の .pcap、
ESP32 の CSV など）。ここでは学習用に、汎用的な「複素CSI行列」の表現と、
NumPy 形式の読み書き、および自作受信機（firmware/csi-rx）の出力形式のパーサを実装する。
他の実機フォーマットのパーサは各ツールのリファレンス実装
（docs/03-implementation/csi-extraction-tools.md 参照）に従って追加する。
"""

from __future__ import annotations

import io as stdio
import json
import re
import warnings
import zlib
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
from numpy.typing import NDArray

# CSI 行列の軸の意味（学習用の標準レイアウト）
#   axis 0: パケット（時間方向のサンプル）
#   axis 1: サブキャリア（周波数方向）
#   axis 2: 空間ストリーム（送受信アンテナ対 = Tx * Rx）
AXIS_PACKET: int = 0
AXIS_SUBCARRIER: int = 1
AXIS_STREAM: int = 2
CSI_NDIM: int = 3  # 期待する次元数


def validate_csi(csi: NDArray[np.complex128]) -> None:
    """複素CSI配列の形状と型を検証する.

    Args:
        csi: 形状 (packets, subcarriers, streams) の複素CSI配列。

    Raises:
        TypeError: 複素数配列でない場合。
        ValueError: 次元数が ``CSI_NDIM`` でない、または空配列の場合。
    """
    if not np.iscomplexobj(csi):
        raise TypeError(f"CSI は複素配列である必要があります: dtype={csi.dtype}")
    if csi.ndim != CSI_NDIM:
        raise ValueError(
            f"CSI は {CSI_NDIM} 次元 (packets, subcarriers, streams) が必要です: "
            f"ndim={csi.ndim}, shape={csi.shape}"
        )
    if csi.size == 0:
        raise ValueError("CSI 配列が空です")


def load_npz(path: str | Path) -> NDArray[np.complex128]:
    """``save_npz`` で保存した複素CSI配列を読み込む.

    Args:
        path: .npz ファイルのパス。

    Returns:
        形状 (packets, subcarriers, streams) の複素CSI配列。

    Raises:
        FileNotFoundError: ファイルが存在しない場合。
        KeyError: 期待するキー "csi" が含まれない場合。
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"CSI ファイルが見つかりません: {path}")
    with np.load(path) as data:
        if "csi" not in data:
            raise KeyError(f"キー 'csi' が .npz に存在しません: keys={list(data.keys())}")
        csi: NDArray[np.complex128] = data["csi"].astype(np.complex128)
    validate_csi(csi)
    return csi


def save_npz(path: str | Path, csi: NDArray[np.complex128]) -> None:
    """複素CSI配列を .npz として保存する.

    Args:
        path: 保存先パス。
        csi: 形状 (packets, subcarriers, streams) の複素CSI配列。
    """
    validate_csi(csi)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, csi=csi)


# ---------------------------------------------------------------------------
# firmware/csi-rx の出力（format=1 / format=2 / format=3）
#
#   format=1: CSI,<rx_us>,<rssi_dbm>,<noise_floor_dbm>,<channel>,<secondary>,<sig_mode>,<mcs>,
#             <cwb>,<stbc>,<sgi>,<first_word_invalid>,<len>,<hex>*<crc32>
#   format=2: CSI,<rx_us>,<seq>,<rssi_dbm>,...（以下 format=1 と同じ）
#   format=3: CSI,<rx_us>,<tx>,<seq>,<rssi_dbm>,...,<first_word_invalid>,<ltf>,<len>,<hex>*<crc32>
#
# <seq> は送信機の連番（受信機で対応が取れなければ -1）。列数で版を見分ける。
# format=3（2026-09-23・送信機 2 台）: <tx> は送信元の MAC（12 桁の 16 進）、
# <ltf> は <hex> に入れた区間
# （htltf = HT-LTF だけ / lltf = LLTF だけ / raw = 生のまま）。1 区間 = 128 バイト。
# format=1・2 の <hex> は LLTF + HT-LTF の 256 バイト（下）。
# <hex> は int8 の CSI 生データ。2 byte で 1 サブキャリア、[虚部, 実部] の順。
# 送信機が HT20・非 STBC のとき len=256 = LLTF 64 + HT-LTF 64 サブキャリア。
# 各区間内の並びは周波数番号 0..31, -32..-1（ESP-IDF の資料どおり。2026-09-23 に
# 実データで DC・ガード帯域の 0 の位置が一致することを確認）。
# ---------------------------------------------------------------------------

RX_LINE_PREFIX: str = "CSI,"
RX_COMMENT_PREFIX: str = "#"
RX_CRC_SEPARATOR: str = "*"
RX_CRC_HEX_DIGITS: int = 8
RX_N_FIELDS_V1: int = 14  # "CSI" + 見出し 12 列 + hex
RX_N_FIELDS_V2: int = 15  # format=1 + seq
RX_FIELD_SEQ_V2: int = 2
RX_N_FIELDS_V3: int = 17  # format=2 + tx + ltf
RX_FIELD_TX_V3: int = 2
RX_FIELD_LTF_V3: int = 14  # format=3 の区間の列（CSI,rx_us,tx,seq,…,first_word_invalid の次）
SECTION_BOTH: str = "both"  # format=1・2: LLTF + HT-LTF の両方が入っている
SECTIONS_V3: tuple[str, ...] = ("htltf", "lltf", "raw")
MAC_HEX_DIGITS: int = 12
SEQ_UNKNOWN: int = -1  # 連番が無い（format=1）または対応が取れなかった
SEQ_MODULUS: int = 2**32  # 送信機の連番は uint32

BYTES_PER_SUBCARRIER: int = 2  # [虚部, 実部] の int8 × 2
N_SUBCARRIERS_PER_LTF: int = 64  # 20 MHz の FFT 点数
HT20_LEN_BYTES: int = 2 * N_SUBCARRIERS_PER_LTF * BYTES_PER_SUBCARRIER  # LLTF + HT-LTF = 256
FIRST_WORD_SUBCARRIERS: int = 2  # first_word_invalid のとき無効になる先頭のサブキャリア数

# 使えるサブキャリアの周波数番号の上限（DC=0 は常に空）
#   LLTF（802.11a/g 相当）: ±1..±26、HT-LTF（802.11n HT20）: ±1..±28
LLTF_MAX_INDEX: int = 26
HTLTF_MAX_INDEX: int = 28

RX_TIMESTAMP_MODULUS: int = 2**32  # rx_us は 32bit（約 71.6 分で一周）

Ltf = Literal["lltf", "htltf"]


class RxCrcError(ValueError):
    """CRC が一致しない行（USB 転送で壊れた行）."""


class RxTruncatedTailWarning(UserWarning):
    """記録の最後の行が途中で切れていたので捨てた（SD への書き込みの途中で止まった記録）."""


@dataclass(frozen=True)
class RxLine:
    """受信機の 1 行（1 パケット）.

    Attributes:
        rx_us: 受信時刻 [µs]（受信機ローカル・32bit で一周する）。
        seq: 送信機の連番（不明なら ``SEQ_UNKNOWN``）。
        rssi_dbm: RSSI [dBm]。
        noise_floor_dbm: 雑音レベル [dBm]。
        channel: 一次チャンネル。
        secondary: 二次チャンネル（0: なし / 1: 上 / 2: 下）。
        sig_mode: 0: 11bg / 1: HT(11n)。
        mcs: MCS（HT のみ有効）。
        cwb: 0: 20 MHz / 1: 40 MHz。
        stbc: 0: 非 STBC / 1: STBC。
        sgi: 0: Long GI / 1: Short GI。
        first_word_invalid: True なら先頭 2 サブキャリア分が無効。
        raw: CSI 生データ（int8、長さ len）。
        tx: 送信元の MAC（小文字・コロン区切り。format=1・2 では None）。
        section: ``raw`` に入っている区間（format=1・2 は ``both`` = LLTF + HT-LTF、
            format=3 は ``htltf`` / ``lltf`` / ``raw``）。
    """

    rx_us: int
    seq: int
    rssi_dbm: int
    noise_floor_dbm: int
    channel: int
    secondary: int
    sig_mode: int
    mcs: int
    cwb: int
    stbc: int
    sgi: int
    first_word_invalid: bool
    raw: NDArray[np.int8]
    tx: str | None = None
    section: str = SECTION_BOTH


def colon_mac(mac: str) -> str:
    """MAC を小文字・コロン区切りにそろえる（``02000000000b`` / ``24:0a:…`` のどちらも受け付ける）.

    Args:
        mac: MAC。

    Returns:
        ``02:00:00:00:00:0b`` の形。

    Raises:
        ValueError: 12 桁の 16 進でない場合。
    """
    digits = mac.replace(":", "").lower()
    if len(digits) != MAC_HEX_DIGITS or any(c not in "0123456789abcdef" for c in digits):
        raise ValueError(f"MAC ではありません: {mac!r}")
    return ":".join(digits[i:i + 2] for i in range(0, MAC_HEX_DIGITS, 2))


def parse_rx_line(line: str) -> RxLine:
    """受信機の 1 行を検証して解析する.

    Args:
        line: ``CSI,`` で始まる 1 行（末尾の改行はあってもよい）。

    Returns:
        解析結果。

    Raises:
        RxCrcError: CRC が無い・一致しない場合（転送中に壊れた行）。
        ValueError: 接頭辞・列数・長さ・16 進のいずれかが不正な場合。
    """
    line = line.rstrip("\r\n")
    if not line.startswith(RX_LINE_PREFIX):
        raise ValueError(f"CSI 行ではありません: {line[:20]!r}")
    body, sep, crc_hex = line.rpartition(RX_CRC_SEPARATOR)
    if not sep or len(crc_hex) != RX_CRC_HEX_DIGITS:
        raise RxCrcError("CRC が付いていません（途中で切れた行）")
    try:
        crc_expected = int(crc_hex, 16)
    except ValueError as e:
        raise RxCrcError(f"CRC が 16 進ではありません: {crc_hex!r}") from e
    if zlib.crc32(body.encode("ascii", errors="replace")) != crc_expected:
        raise RxCrcError("CRC が一致しません（転送中に壊れた行）")

    fields = body.split(",")
    tx: str | None = None
    section = SECTION_BOTH
    if len(fields) == RX_N_FIELDS_V3:
        section = fields.pop(RX_FIELD_LTF_V3)  # 後ろの列から取り除く（前の位置がずれないように）
        tx = colon_mac(fields.pop(RX_FIELD_TX_V3))
        if section not in SECTIONS_V3:
            raise ValueError(f"区間が {SECTIONS_V3} のどれでもありません: {section!r}")
        seq_field = fields.pop(RX_FIELD_SEQ_V2)
    elif len(fields) == RX_N_FIELDS_V2:
        seq_field = fields.pop(RX_FIELD_SEQ_V2)  # 以降は format=1 と同じ並びになる
    elif len(fields) == RX_N_FIELDS_V1:
        seq_field = str(SEQ_UNKNOWN)
    else:
        raise ValueError(
            f"列数が {RX_N_FIELDS_V1}（format=1）・{RX_N_FIELDS_V2}（format=2）・"
            f"{RX_N_FIELDS_V3}（format=3）のどれでもありません: {len(fields)}"
        )
    try:
        seq = int(seq_field)
        header = [int(v) for v in fields[1:-1]]
        raw = np.frombuffer(bytes.fromhex(fields[-1]), dtype=np.int8)
    except ValueError as e:
        raise ValueError(f"数値または 16 進が不正です: {e}") from e
    if seq < SEQ_UNKNOWN:
        raise ValueError(f"seq が不正です: {seq}")
    (rx_us, rssi, noise, channel, secondary, sig_mode, mcs, cwb, stbc, sgi, fwi, length) = header
    if raw.size != length:
        raise ValueError(f"len={length} と CSI の長さ {raw.size} が一致しません")
    return RxLine(
        rx_us=rx_us,
        seq=seq,
        rssi_dbm=rssi,
        noise_floor_dbm=noise,
        channel=channel,
        secondary=secondary,
        sig_mode=sig_mode,
        mcs=mcs,
        cwb=cwb,
        stbc=stbc,
        sgi=sgi,
        first_word_invalid=bool(fwi),
        raw=raw,
        tx=tx,
        section=section,
    )


def raw_to_complex(raw: NDArray[np.int8]) -> NDArray[np.complex128]:
    """[虚部, 実部] の int8 列を複素数列に変換する.

    Args:
        raw: 偶数長の int8 配列。

    Returns:
        長さ ``len(raw) // 2`` の複素配列（生データの並び順のまま）。

    Raises:
        ValueError: 長さが奇数の場合。
    """
    if raw.size % BYTES_PER_SUBCARRIER != 0:
        raise ValueError(f"CSI 生データの長さが偶数ではありません: {raw.size}")
    pairs = raw.astype(np.float64).reshape(-1, BYTES_PER_SUBCARRIER)
    return pairs[:, 1] + 1j * pairs[:, 0]


def raw_position_to_index(position: int) -> int:
    """区間内の位置（0..63）を周波数番号（-32..31）に変換する.

    Args:
        position: 1 区間（64 サブキャリア）内の並び位置。

    Returns:
        サブキャリアの周波数番号。並びは 0..31, -32..-1。

    Raises:
        ValueError: 範囲外の場合。
    """
    if not 0 <= position < N_SUBCARRIERS_PER_LTF:
        raise ValueError(f"位置は 0..{N_SUBCARRIERS_PER_LTF - 1}: {position}")
    half = N_SUBCARRIERS_PER_LTF // 2
    return position if position < half else position - N_SUBCARRIERS_PER_LTF


def valid_subcarrier_index(ltf: Ltf) -> NDArray[np.int64]:
    """区間ごとの使えるサブキャリアの周波数番号を昇順で返す.

    Args:
        ltf: ``"lltf"``（±1..±26）または ``"htltf"``（±1..±28）。

    Returns:
        周波数番号の配列（DC とガード帯域を除く）。
    """
    max_index = LLTF_MAX_INDEX if ltf == "lltf" else HTLTF_MAX_INDEX
    idx = np.arange(-max_index, max_index + 1, dtype=np.int64)
    valid: NDArray[np.int64] = idx[idx != 0]
    return valid


@dataclass(frozen=True)
class RxCapture:
    """受信機の記録を配列にまとめたもの.

    Attributes:
        csi: 形状 (packets, subcarriers, streams=1) の複素CSI。
            サブキャリアは ``subcarrier_index`` の順（周波数の昇順）。
            無効なサブキャリアは NaN。
        subcarrier_index: axis 1 の各サブキャリアの周波数番号。
        time_sec: 先頭パケットからの経過時間 [sec]（32bit の一周を補正済み）。
        seq: 送信機の連番（不明は ``SEQ_UNKNOWN``）。複数の受信機の記録を揃える鍵。
        rssi_dbm: パケットごとの RSSI [dBm]。
        noise_floor_dbm: パケットごとの雑音レベル [dBm]。
        ltf: 取り出した区間。
    """

    csi: NDArray[np.complex128]
    subcarrier_index: NDArray[np.int64]
    time_sec: NDArray[np.float64]
    seq: NDArray[np.int64]
    rssi_dbm: NDArray[np.int64]
    noise_floor_dbm: NDArray[np.int64]
    ltf: Ltf
    tx: str | None = None


def unwrap_rx_us(rx_us: NDArray[np.int64]) -> NDArray[np.int64]:
    """32bit で一周する受信時刻を、先頭からの単調な経過時間 [µs] にする.

    隣り合う 2 パケットの間隔が一周（約 71.6 分）未満であることを前提とする。

    Args:
        rx_us: 受信時刻 [µs] の列。

    Returns:
        先頭を 0 とした経過時間 [µs]。
    """
    if rx_us.size == 0:
        return rx_us.astype(np.int64)
    step_us = np.mod(np.diff(rx_us.astype(np.int64)), RX_TIMESTAMP_MODULUS)
    return np.concatenate([[0], np.cumsum(step_us)]).astype(np.int64)


def estimate_missing(elapsed_us: NDArray[np.int64], interval_us: int) -> int:
    """送信間隔から、受信できなかったパケット数を推定する.

    Args:
        elapsed_us: 単調な経過時間 [µs]（``unwrap_rx_us`` の出力）。
        interval_us: 送信間隔 [µs]。

    Returns:
        推定欠落数（間隔が送信間隔の n 倍なら n-1 個抜けたとみなす）。

    Raises:
        ValueError: ``interval_us`` が正でない場合。
    """
    if interval_us <= 0:
        raise ValueError(f"送信間隔は正の値が必要です: {interval_us}")
    if elapsed_us.size < 2:
        return 0
    slots = np.rint(np.diff(elapsed_us) / interval_us).astype(np.int64)
    return int(np.clip(slots - 1, 0, None).sum())


def count_missing_by_seq(seq: NDArray[np.int64]) -> int | None:
    """送信機の連番の欠番から、受信できなかったパケット数を数える.

    受信時刻から推定する ``estimate_missing`` と違い、送信の遅れや詰まりに左右されない。
    連番が不明（``SEQ_UNKNOWN``）の行は除いて数える。

    Args:
        seq: 受信順の連番。

    Returns:
        欠番の数。連番の分かる行が 2 行未満なら None。

    Raises:
        ValueError: 連番が増えていない箇所がある場合（重複・逆行）。
            一周（2**32）の半分を超える増分は逆行とみなす。
    """
    known = seq[seq != SEQ_UNKNOWN].astype(np.int64)
    if known.size < 2:
        return None
    step = np.mod(np.diff(known), SEQ_MODULUS)
    if np.any(step == 0):
        raise ValueError("連番が重複しています（同じパケットが 2 回、または対応づけの誤り）")
    if np.any(step > SEQ_MODULUS // 2):
        raise ValueError("連番が逆行しています（受信順の乱れ、または対応づけの誤り）")
    return int((step - 1).sum())


def _ltf_to_frequency_order(
    segment: NDArray[np.complex128], ltf: Ltf, first_word_invalid: bool
) -> NDArray[np.complex128]:
    """1 区間（64 サブキャリア・生の並び）から使えるサブキャリアを周波数順に取り出す."""
    seg = segment.copy()
    if first_word_invalid and ltf == "lltf":
        seg[:FIRST_WORD_SUBCARRIERS] = np.nan  # 先頭 4 byte は LLTF の 0 番・+1 番
    by_index = {raw_position_to_index(p): seg[p] for p in range(N_SUBCARRIERS_PER_LTF)}
    return np.array([by_index[int(k)] for k in valid_subcarrier_index(ltf)])


def lines_to_capture(lines: list[RxLine], ltf: Ltf = "htltf") -> RxCapture:
    """解析済みの行をまとめて配列にする.

    Args:
        lines: ``parse_rx_line`` の結果（受信順）。
        ltf: 取り出す区間。既定は HT-LTF（56 サブキャリア・先頭無効の影響なし）。

    Returns:
        まとめた記録。

    Raises:
        ValueError: 行が空、HT20・非 STBC 以外のパケットを含む、または長さが揃っていない場合。
    """
    if not lines:
        raise ValueError("CSI 行がありません")
    ltf_bytes = N_SUBCARRIERS_PER_LTF * BYTES_PER_SUBCARRIER
    for n, rec in enumerate(lines):
        if rec.section == SECTION_BOTH:
            if rec.raw.size != HT20_LEN_BYTES or rec.cwb != 0 or rec.stbc != 0:
                raise ValueError(
                    f"{n} 行目: HT20・非 STBC（len={HT20_LEN_BYTES}）以外は未対応です: "
                    f"len={rec.raw.size} cwb={rec.cwb} stbc={rec.stbc}"
                )
        elif rec.section != ltf or rec.raw.size != ltf_bytes:
            raise ValueError(
                f"{n} 行目: この行の区間は {rec.section}（{rec.raw.size} バイト）で、"
                f"{ltf} を取り出せません（11g の送信機は ltf='lltf' で読む）"
            )
    offset = 0 if ltf == "lltf" else N_SUBCARRIERS_PER_LTF
    rows = []
    for rec in lines:
        sc = raw_to_complex(rec.raw)
        start = offset if rec.section == SECTION_BOTH else 0
        rows.append(
            _ltf_to_frequency_order(
                sc[start : start + N_SUBCARRIERS_PER_LTF], ltf, rec.first_word_invalid
            )
        )
    csi = np.stack(rows)[:, :, np.newaxis]  # streams=1（送受とも 1 アンテナ）
    elapsed_us = unwrap_rx_us(np.array([rec.rx_us for rec in lines], dtype=np.int64))
    return RxCapture(
        csi=csi,
        subcarrier_index=valid_subcarrier_index(ltf),
        time_sec=elapsed_us / 1e6,
        seq=np.array([rec.seq for rec in lines], dtype=np.int64),
        rssi_dbm=np.array([rec.rssi_dbm for rec in lines], dtype=np.int64),
        noise_floor_dbm=np.array([rec.noise_floor_dbm for rec in lines], dtype=np.int64),
        ltf=ltf,
        tx=lines[0].tx if len({rec.tx for rec in lines}) == 1 else None,
    )


# 受信機の見出しから主の送信機を読む（format=3 の "# tx id=0 mac=…"、または "# mac=… tx_mac=…"）
PRIMARY_TX_PATTERN = re.compile(
    r"^# (?:tx id=0 mac=([0-9a-f]{12})|mac=[0-9a-f]{12} .*?tx_mac=([0-9a-f]{12}))")


# 受信機の見出しの送信機の一覧（format=3。name= は 2026-09-23 から）
TX_HEADER_PATTERN = re.compile(r"^# tx id=(\d+) mac=([0-9a-f]{12})(?: name=(\S+))?")


def parse_tx_header(line: str) -> tuple[str, str | None] | None:
    """見出しの ``# tx id=… mac=… name=…`` 行から (MAC, 機種名) を読む.

    Args:
        line: 注記の 1 行。

    Returns:
        (小文字・コロン区切りの MAC, 機種名 or None)。送信機の見出しでなければ None。
    """
    m = TX_HEADER_PATTERN.match(line)
    if m is None:
        return None
    return colon_mac(m.group(2)), m.group(3)


def load_rx_csv(path: str | Path, ltf: Ltf = "htltf", tx: str | None = None) -> RxCapture:
    """``scripts/capture_csi.py`` が保存したファイルを読み込む.

    ``#`` で始まる行は注記として読み飛ばす。保存時に CRC 不一致の行は除いているため、
    ここで不正な行があればファイル自体の破損とみなして例外にする。

    送信機が 2 台以上の記録（format=3）では ``tx`` で 1 台を選ぶ。選ばなければ、受信機の見出しに
    ある主の送信機（``# tx id=0 mac=…`` または ``# mac=… tx_mac=…``）を読む（既存の解析は
    そのまま動く）。
    11g の送信機（LLTF だけ）は ``ltf="lltf"`` で読む。

    Args:
        path: 記録ファイルのパス。
        ltf: 取り出す区間（``lines_to_capture`` 参照）。
        tx: 送信機の MAC（``24:0a:…`` / ``240a…`` のどちらでも）。None なら上の規則で決める。

    Returns:
        まとめた記録。

    Raises:
        FileNotFoundError: ファイルが存在しない場合。
        ValueError: 不正な行を含む、CSI 行が無い、選んだ送信機の行が無い、
            送信機が複数あるのに主の送信機が分からない場合（行番号つき）。
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"CSI ファイルが見つかりません: {path}")
    lines: list[RxLine] = []
    primary: str | None = None
    with open_rx_text(path) as f:
        for lineno, text in enumerate(f, start=1):
            if not text.strip():
                continue
            if text.startswith(RX_COMMENT_PREFIX):
                m = PRIMARY_TX_PATTERN.match(text)
                if m and primary is None:
                    primary = colon_mac(m.group(1) or m.group(2))
                continue
            try:
                lines.append(parse_rx_line(text))
            except ValueError as e:
                raise ValueError(f"{path}:{lineno}: {e}") from e
    present = sorted({rec.tx for rec in lines if rec.tx is not None})
    want = colon_mac(tx) if tx is not None else None
    if want is None and len(present) > 1:
        if primary is None or primary not in present:
            raise ValueError(f"{path}: 送信機が複数あります（{present}）。tx で選んでください")
        want = primary
    if want is not None:
        lines = [rec for rec in lines if rec.tx == want]
        if not lines:
            raise ValueError(f"{path}: 送信機 {want} の CSI 行がありません（あるのは {present}）")
    return lines_to_capture(lines, ltf=ltf)


def transmitters_in(path: str | Path) -> dict[str, int]:
    """記録に含まれる送信機ごとの CSI 行の数を返す（format=3。それより前の形式は空）.

    Args:
        path: 記録ファイルのパス。

    Returns:
        送信機の MAC → 行数。

    Raises:
        FileNotFoundError: ファイルが存在しない場合。
    """
    counts: dict[str, int] = {}
    with open_rx_text(path) as f:
        for text in f:
            if text.startswith(RX_LINE_PREFIX):
                rec = parse_rx_line(text)
                if rec.tx is not None:
                    counts[rec.tx] = counts.get(rec.tx, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# 記録の保管形式（ADR 0004）
#   <記録名>.csv       記録ツールが書く元の形
#   <記録名>.csv.zst   元の CSV を zstd で圧縮したもの（中身はバイト単位で同じ。data/raw の保管）
#   <記録名>.<ltf>.parquet  load_rx_csv の結果（RxCapture）の写し
#                      （data/processed。消しても作り直せる）
# ---------------------------------------------------------------------------
RAW_CSV_SUFFIX: str = ".csv"
RAW_ZST_SUFFIX: str = ".csv.zst"
RAW_SUFFIXES: tuple[str, ...] = (RAW_ZST_SUFFIX, RAW_CSV_SUFFIX)  # 長い方から照合する
PARQUET_SUFFIX: str = ".parquet"
PARQUET_SCHEMA_VERSION: str = "1"
CSI_NAN_INT16: int = -32768  # Parquet で NaN（無効なサブキャリア）を表す値（成分は int8 の範囲）


def recording_name(path: str | Path) -> str:
    """記録ファイルのパスから記録名を返す（``.csv``・``.csv.zst``・``.<ltf>.parquet`` を除く）.

    ``Path.stem`` は ``x.csv.zst`` を ``x.csv`` にするので、記録名にはこれを使う。

    Args:
        path: 記録ファイル（またはその写し）のパス。

    Returns:
        記録名（例: ``20261007-001359-exp11-sleep-01``）。
    """
    name = Path(path).name
    if name.endswith(PARQUET_SUFFIX):
        stem = name[: -len(PARQUET_SUFFIX)]
        for ltf in ("lltf", "htltf"):
            if stem.endswith(f".{ltf}"):
                return stem[: -len(ltf) - 1]
        return stem
    for suffix in RAW_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return Path(name).stem


def find_raw(raw_dir: str | Path, recording: str) -> Path:
    """記録名から記録ファイル（``.csv.zst`` か ``.csv``）を探す.

    Args:
        raw_dir: 記録の置き場。
        recording: 記録名。

    Returns:
        見つかったファイル（両方あれば ``.csv.zst``）。

    Raises:
        FileNotFoundError: どちらも無い場合。
    """
    for suffix in RAW_SUFFIXES:
        path = Path(raw_dir) / f"{recording}{suffix}"
        if path.exists():
            return path
    raise FileNotFoundError(f"記録がありません: {Path(raw_dir) / recording}{{.csv,.csv.zst}}")


def _drop_truncated_tail(lines: Iterable[str], path: Path) -> Iterator[str]:
    """改行で終わらない最後の CSI 行が切れていれば捨てる（警告を出す）.

    SD の記録は電源を切る・カードが書けなくなると、最後の行が途中で終わる（2026-10-07・08 の
    S0001）。その 1 行のためにファイル全体を読めなくしないよう、ここで捨てる。途中の行が
    壊れていれば、これまでどおり読み手が例外にする。

    Args:
        lines: ファイルの行。
        path: 記録ファイル（警告の文に使う）。

    Yields:
        行（末尾の改行つき。最後の行だけは改行がないことがある）。
    """
    prev: str | None = None
    for text in lines:
        if prev is not None:
            yield prev
        prev = text
    if prev is None:
        return
    if not prev.endswith("\n") and prev.startswith(RX_LINE_PREFIX):
        try:
            parse_rx_line(prev)
        except RxCrcError:
            warnings.warn(f"{path}: 最後の行が途中で切れているので捨てました（{len(prev)} 文字）",
                          RxTruncatedTailWarning, stacklevel=2)
            return
    yield prev


@contextmanager
def open_rx_text(path: str | Path) -> Iterator[Iterator[str]]:
    """記録ファイルを行ごとに読む（``.csv.zst`` は展開しながら読む）.

    ASCII として読み、読めない文字は置き換える（注記の行に別の文字が混じっても止まらない。
    CSI の行は CRC で確かめるので、壊れた行は取り込まれない）。
    改行で終わらない最後の CSI 行が途中で切れていれば、捨てて ``RxTruncatedTailWarning`` を出す
    （どの読み手にも同じ行が見えるよう、ここで 1 回だけ捨てる）。

    Args:
        path: 記録ファイル。

    Yields:
        行（末尾の改行つき）を順に返すもの。

    Raises:
        FileNotFoundError: ファイルが無い場合。
    """
    path = Path(path)
    if path.name.endswith(RAW_ZST_SUFFIX):
        import zstandard

        with path.open("rb") as raw, zstandard.ZstdDecompressor().stream_reader(raw) as zs:
            yield _drop_truncated_tail(
                stdio.TextIOWrapper(zs, encoding="ascii", errors="replace"), path)
    else:
        with path.open(encoding="ascii", errors="replace") as f:
            yield _drop_truncated_tail(f, path)


def parquet_path(out_dir: str | Path, recording: str, ltf: Ltf = "htltf") -> Path:
    """Parquet の写しのパスを返す.

    Args:
        out_dir: 置き場。
        recording: 記録名。
        ltf: 区間。

    Returns:
        ``<out_dir>/<記録名>.<ltf>.parquet``。
    """
    return Path(out_dir) / f"{recording}.{ltf}{PARQUET_SUFFIX}"


def save_rx_parquet(cap: RxCapture, path: str | Path, source: str = "") -> None:
    """``RxCapture`` を Parquet に書く（読み戻すと同じ値になる）.

    Args:
        cap: 記録。
        path: 書き出し先。
        source: 元の記録ファイルの名前（メタデータに残す）。

    Raises:
        ValueError: CSI の成分が整数でない（int8 の受信機の出力でない）場合。
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    n, n_sc, n_st = cap.csi.shape
    parts = []
    for comp in (cap.csi.real, cap.csi.imag):
        nan = np.isnan(comp)
        vals = np.where(nan, 0, comp)
        if np.any(vals != np.round(vals)) or np.any(np.abs(vals) > np.iinfo(np.int8).max + 1):
            raise ValueError("CSI の成分が int8 の整数ではありません（受信機の出力以外は未対応）")
        parts.append(np.where(nan, CSI_NAN_INT16, vals).astype(np.int16).reshape(n, n_sc * n_st))
    width = n_sc * n_st
    table = pa.table({
        "time_sec": pa.array(cap.time_sec, pa.float64()),
        "seq": pa.array(cap.seq, pa.int64()),
        "rssi_dbm": pa.array(cap.rssi_dbm, pa.int16()),
        "noise_floor_dbm": pa.array(cap.noise_floor_dbm, pa.int16()),
        "csi_re": pa.FixedSizeListArray.from_arrays(pa.array(parts[0].ravel()), width),
        "csi_im": pa.FixedSizeListArray.from_arrays(pa.array(parts[1].ravel()), width),
    })
    meta = {
        "schema_version": PARQUET_SCHEMA_VERSION,
        "ltf": cap.ltf,
        "tx": cap.tx or "",
        "subcarrier_index": json.dumps(cap.subcarrier_index.tolist()),
        "n_streams": str(n_st),
        "source": source,
    }
    table = table.replace_schema_metadata({f"csikit.{k}": v for k, v in meta.items()})
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    pq.write_table(table, tmp, compression="zstd")
    tmp.replace(path)  # 書きかけのファイルを写しとして残さない


def load_rx_parquet(path: str | Path) -> RxCapture:
    """``save_rx_parquet`` で書いた Parquet を ``RxCapture`` に読み戻す.

    Args:
        path: Parquet のパス。

    Returns:
        記録。

    Raises:
        FileNotFoundError: ファイルが無い場合。
        ValueError: この形式で書いたものでない・版が違う場合。
    """
    import pyarrow.parquet as pq

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Parquet が見つかりません: {path}")
    table = pq.read_table(path)
    raw_meta = table.schema.metadata or {}
    meta = {k.decode().removeprefix("csikit."): v.decode() for k, v in raw_meta.items()
            if k.startswith(b"csikit.")}
    if meta.get("schema_version") != PARQUET_SCHEMA_VERSION:
        raise ValueError(
            f"{path}: csikit の Parquet（版 {PARQUET_SCHEMA_VERSION}）ではありません")
    ltf = meta["ltf"]
    if ltf not in ("lltf", "htltf"):
        raise ValueError(f"{path}: 区間が不正です: {ltf}")
    sc_index = np.array(json.loads(meta["subcarrier_index"]), dtype=np.int64)
    n_st = int(meta["n_streams"])
    n = table.num_rows

    def comp(name: str) -> NDArray[np.float64]:
        flat = table.column(name).combine_chunks().flatten().to_numpy().astype(np.float64)
        flat[flat == CSI_NAN_INT16] = np.nan
        return np.asarray(flat.reshape(n, len(sc_index), n_st), dtype=np.float64)

    return RxCapture(
        csi=comp("csi_re") + 1j * comp("csi_im"),
        subcarrier_index=sc_index,
        time_sec=table.column("time_sec").to_numpy().astype(np.float64),
        seq=table.column("seq").to_numpy().astype(np.int64),
        rssi_dbm=table.column("rssi_dbm").to_numpy().astype(np.int64),
        noise_floor_dbm=table.column("noise_floor_dbm").to_numpy().astype(np.int64),
        ltf=ltf,
        tx=meta["tx"] or None,
    )


def load_rx(path: str | Path, ltf: Ltf = "htltf", tx: str | None = None) -> RxCapture:
    """記録を、形式（``.csv``・``.csv.zst``・``.parquet``）に合わせて読む.

    Args:
        path: 記録ファイルか、その Parquet の写し。
        ltf: 取り出す区間（Parquet は書いたときの区間と一致しなければ ValueError）。
        tx: 送信機の MAC（同上。None なら確かめない）。

    Returns:
        記録。

    Raises:
        FileNotFoundError: ファイルが無い場合。
        ValueError: 読めない・Parquet の区間や送信機が求めるものと違う場合。
    """
    if str(path).endswith(PARQUET_SUFFIX):
        cap = load_rx_parquet(path)
        if cap.ltf != ltf:
            raise ValueError(f"{path}: 区間が違います（写しは {cap.ltf}、求めたのは {ltf}）")
        if tx is not None and cap.tx != colon_mac(tx):
            raise ValueError(f"{path}: 送信機が違います（写しは {cap.tx}、求めたのは {tx}）")
        return cap
    return load_rx_csv(path, ltf=ltf, tx=tx)


# 受信機の # stats 行に載る送信機の出力（firmware/csi-rx・2026-10-07 から）。
# 負の値は「まだ分からない」
PEER_TX_POWER_PATTERN = re.compile(r"\bpeer_tx_power_dbm=(-?\d+(?:\.\d+)?)\b")
FORMAT3_TX_FIELD = 2  # format=3 の CSI 行で送信元（MAC）の列


def peer_tx_power_per_row(path: str | Path, tx: str | None = None) -> NDArray[np.float64]:
    """記録の各 CSI 行に、直前の ``# stats`` 行に載った送信機の出力 [dBm] を割り当てる.

    ``load_rx_csv(path, tx=...)`` の行と同じ並び・同じ数になる
    （``tx`` は ``RxCapture.tx`` を渡す）。
    ``# stats`` は 1 秒ごとなので、出力の切り替わりの時刻は最大 1 秒ずれる。

    Args:
        path: 記録ファイル（``.csv`` / ``.csv.zst``）。
        tx: 送信機の MAC（None なら全ての CSI 行）。

    Returns:
        形状 (rows,) の出力 [dBm]。分からない行（最初の ``# stats`` より前・受信機が古い・
        送信機の版が古い）は NaN。

    Raises:
        FileNotFoundError: ファイルが無い場合。
    """
    want = colon_mac(tx) if tx is not None else None
    out: list[float] = []
    cur = float("nan")
    with open_rx_text(path) as f:
        for text in f:
            if text.startswith(RX_LINE_PREFIX):
                if want is not None:
                    fields = text.split(",", FORMAT3_TX_FIELD + 1)
                    field = fields[FORMAT3_TX_FIELD] if len(fields) > FORMAT3_TX_FIELD else ""
                    if len(field) != MAC_HEX_DIGITS or colon_mac(field) != want:
                        continue
                out.append(cur)
            elif text.startswith("# stats"):
                m = PEER_TX_POWER_PATTERN.search(text)
                if m:
                    v = float(m.group(1))
                    cur = v if v >= 0 else float("nan")
    return np.asarray(out, dtype=np.float64)
