"""firmware/csi-rx の出力パーサのテスト.

実機の出力と同じ形の行を合成して、検証・並べ替え・時刻補正が期待どおりかを確かめる。
"""

from __future__ import annotations

import warnings
import zlib
from pathlib import Path

import numpy as np
import pytest

from csikit import io

N_PER_LTF = io.N_SUBCARRIERS_PER_LTF
INTERVAL_US = 10_000  # 送信間隔 10 ms
# 実機（2026-09-23）と同じ見出し: ch11・HT・MCS0・20 MHz・非 STBC・Long GI
HEADER = dict(rssi=-52, noise=-96, channel=11, secondary=0, sig_mode=1, mcs=0, cwb=0, stbc=0,
              sgi=0)


def make_raw(fill: float = 0.0) -> np.ndarray:
    """生の並びの位置 p に「実部 = p, 虚部 = -p + fill」を入れた 256 byte を作る.

    位置から値が一意に決まるので、並べ替えの結果から元の位置を逆算できる。
    """
    imag_real = []
    for segment in range(2):  # LLTF, HT-LTF
        for p in range(N_PER_LTF):
            real = p if segment == 0 else p + 64 - 128  # HT-LTF は -64..-1 で区別する
            imag_real += [int(-real + fill), int(real)]
    return np.array(imag_real, dtype=np.int8)


def make_line(rx_us: int, raw: np.ndarray, first_word_invalid: int = 0,
              length: int | None = None, seq: int | None = None) -> str:
    """CRC を付けた受信機の 1 行を作る（seq を渡すと format=2、省略で format=1）."""
    h = HEADER
    seq_col = "" if seq is None else f"{seq},"
    body = (f"CSI,{rx_us},{seq_col}{h['rssi']},{h['noise']},{h['channel']},{h['secondary']},"
            f"{h['sig_mode']},{h['mcs']},{h['cwb']},{h['stbc']},{h['sgi']},{first_word_invalid},"
            f"{raw.size if length is None else length},{raw.tobytes().hex()}")
    return f"{body}*{zlib.crc32(body.encode()):08x}\n"


def test_parse_roundtrip() -> None:
    """見出しと生データがそのまま取り出せる."""
    raw = make_raw()
    rec = io.parse_rx_line(make_line(123_456, raw, first_word_invalid=1))
    assert rec.rx_us == 123_456
    assert rec.rssi_dbm == -52 and rec.noise_floor_dbm == -96 and rec.channel == 11
    assert rec.first_word_invalid is True
    np.testing.assert_array_equal(rec.raw, raw)


def test_parse_rejects_corrupted_line() -> None:
    """1 文字でも変われば CRC で弾かれる."""
    line = make_line(1, make_raw())
    corrupted = line.replace(",-96,", ",-97,", 1)
    with pytest.raises(ValueError, match="CRC"):
        io.parse_rx_line(corrupted)


def test_parse_rejects_truncated_line() -> None:
    """途中で切れた行（CRC が無い）は弾かれる."""
    with pytest.raises(ValueError, match="CRC"):
        io.parse_rx_line(make_line(1, make_raw())[:300])


def test_parse_rejects_length_mismatch() -> None:
    """見出しの len 列と実際の長さが違えば弾かれる（CRC は正しくても）."""
    with pytest.raises(ValueError, match="len=250"):
        io.parse_rx_line(make_line(1, make_raw(), length=250))


def test_raw_to_complex_order_is_imag_then_real() -> None:
    """2 byte は [虚部, 実部] の順."""
    sc = io.raw_to_complex(np.array([3, 4, -1, 2], dtype=np.int8))
    np.testing.assert_array_equal(sc, [4 + 3j, 2 - 1j])


def test_raw_position_to_index() -> None:
    """並びは 0..31, -32..-1."""
    assert [io.raw_position_to_index(p) for p in (0, 31, 32, 63)] == [0, 31, -32, -1]
    with pytest.raises(ValueError):
        io.raw_position_to_index(64)


def test_capture_htltf_frequency_order() -> None:
    """HT-LTF は ±1..±28 の 56 本が周波数の昇順に並ぶ."""
    lines = [io.parse_rx_line(make_line(t * INTERVAL_US, make_raw())) for t in range(3)]
    cap = io.lines_to_capture(lines, ltf="htltf")
    assert cap.csi.shape == (3, 56, 1)
    assert cap.subcarrier_index[0] == -28 and cap.subcarrier_index[-1] == 28
    assert 0 not in cap.subcarrier_index
    # 周波数番号 k の値は、HT-LTF の位置 p = k mod 64 から作った実部 p - 64
    for col, k in enumerate(cap.subcarrier_index):
        p = int(k) % N_PER_LTF
        assert cap.csi[0, col, 0].real == p - 64


def test_capture_lltf_drops_first_word_when_invalid() -> None:
    """first_word_invalid のとき、LLTF の +1 番は NaN になる（0 番は元から含めない）."""
    lines = [io.parse_rx_line(make_line(0, make_raw(), first_word_invalid=1))]
    cap = io.lines_to_capture(lines, ltf="lltf")
    assert cap.csi.shape == (1, 52, 1)
    col_plus1 = int(np.where(cap.subcarrier_index == 1)[0][0])
    assert np.isnan(cap.csi[0, col_plus1, 0])
    assert np.isfinite(np.delete(cap.csi[0, :, 0], col_plus1)).all()


def test_capture_rejects_non_ht20() -> None:
    """HT20・非 STBC 以外（長さ違い）は未対応として弾く."""
    short = io.parse_rx_line(make_line(0, make_raw()[:128]))
    with pytest.raises(ValueError, match="HT20"):
        io.lines_to_capture([short])


def test_unwrap_rx_us_across_32bit_wrap() -> None:
    """32bit の一周をまたいでも経過時間は単調に増える."""
    start = io.RX_TIMESTAMP_MODULUS - INTERVAL_US
    rx_us = np.array([start, (start + INTERVAL_US) % io.RX_TIMESTAMP_MODULUS,
                      (start + 2 * INTERVAL_US) % io.RX_TIMESTAMP_MODULUS])
    np.testing.assert_array_equal(io.unwrap_rx_us(rx_us), [0, INTERVAL_US, 2 * INTERVAL_US])


def test_estimate_missing() -> None:
    """間隔が 2 倍なら 1 個、3 倍なら 2 個欠けたとみなす."""
    elapsed = np.array([0, 10_000, 30_000, 60_000, 70_000])
    assert io.estimate_missing(elapsed, INTERVAL_US) == 3
    with pytest.raises(ValueError):
        io.estimate_missing(elapsed, 0)


def test_load_rx_csv_skips_comments(tmp_path: Path) -> None:
    """# 行を読み飛ばし、時刻は秒になる."""
    path = tmp_path / "cap.csv"
    path.write_text("# capture label=test\n" + make_line(1_000, make_raw())
                    + "# stats rate_hz=100.0\n" + make_line(11_000, make_raw()), encoding="utf-8")
    cap = io.load_rx_csv(path)
    assert cap.csi.shape == (2, 56, 1)
    np.testing.assert_allclose(cap.time_sec, [0.0, 0.01])


def test_load_rx_csv_reports_line_number(tmp_path: Path) -> None:
    """保存済みファイルの壊れた行は、行番号つきで例外になる."""
    path = tmp_path / "cap.csv"
    path.write_text("# header\n" + make_line(0, make_raw()) + "CSI,broken\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"cap\.csv:3"):
        io.load_rx_csv(path)


def write_truncated(path: Path, n_packets: int = 5) -> Path:
    """最後の行が改行も CRC もないまま途中で終わる記録を書く（SD の書き込みが止まった形）."""
    raw = make_raw()
    lines = [make_line(i * INTERVAL_US, raw, seq=i) for i in range(n_packets + 1)]
    data = "# header\n" + "".join(lines[:-1]) + lines[-1][:40]
    if path.name.endswith(io.RAW_ZST_SUFFIX):
        import zstandard

        path.write_bytes(zstandard.ZstdCompressor().compress(data.encode("ascii")))
    else:
        path.write_text(data, encoding="utf-8")
    return path


@pytest.mark.parametrize("name", ["cut.csv", "cut.csv.zst"])
def test_truncated_last_line_is_dropped_with_warning(tmp_path: Path, name: str) -> None:
    """最後の行だけが途中で切れていれば捨てて読み、警告を出す（.csv.zst も同じ）."""
    path = write_truncated(tmp_path / name)
    with pytest.warns(io.RxTruncatedTailWarning, match="最後の行が途中で切れている"):
        cap = io.load_rx_csv(path)
    assert list(cap.seq) == [0, 1, 2, 3, 4]
    with pytest.warns(io.RxTruncatedTailWarning):
        assert len(io.peer_tx_power_per_row(path)) == len(cap.seq)  # どの読み手にも同じ行


def test_truncated_line_in_the_middle_is_still_an_error(tmp_path: Path) -> None:
    """途中の行が切れていれば、これまでどおり行番号つきの例外にする."""
    raw = make_raw()
    path = tmp_path / "mid.csv"
    path.write_text(make_line(0, raw, seq=0) + make_line(INTERVAL_US, raw, seq=1)[:40] + "\n"
                    + make_line(2 * INTERVAL_US, raw, seq=2), encoding="utf-8")
    with pytest.raises(ValueError, match=r"mid\.csv:2"):
        io.load_rx_csv(path)


def test_complete_last_line_without_newline_is_kept(tmp_path: Path) -> None:
    """改行がなくても CRC の合う最後の行は捨てない（警告も出さない）."""
    raw = make_raw()
    path = tmp_path / "nolf.csv"
    path.write_text(make_line(0, raw, seq=0) + make_line(INTERVAL_US, raw, seq=1).rstrip("\n"),
                    encoding="utf-8")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert list(io.load_rx_csv(path).seq) == [0, 1]


def test_load_rx_csv_missing_file(tmp_path: Path) -> None:
    """存在しないファイルは FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        io.load_rx_csv(tmp_path / "none.csv")


def test_parse_format2_reads_seq() -> None:
    """format=2 は rx_us の次に seq がある。format=1 は seq 不明."""
    v2 = io.parse_rx_line(make_line(5_000, make_raw(), seq=190_332))
    assert v2.seq == 190_332 and v2.rx_us == 5_000 and v2.rssi_dbm == -52
    np.testing.assert_array_equal(v2.raw, make_raw())
    assert io.parse_rx_line(make_line(5_000, make_raw())).seq == io.SEQ_UNKNOWN
    assert io.parse_rx_line(make_line(5_000, make_raw(), seq=-1)).seq == io.SEQ_UNKNOWN


def test_parse_distinguishes_crc_from_format_errors() -> None:
    """壊れた行は RxCrcError、CRC は正しいが列数が違う行はただの ValueError."""
    with pytest.raises(io.RxCrcError):
        io.parse_rx_line(make_line(1, make_raw())[:300])
    body = "CSI,1,2,3"
    with pytest.raises(ValueError, match="列数") as info:
        io.parse_rx_line(f"{body}*{zlib.crc32(body.encode()):08x}")
    assert not isinstance(info.value, io.RxCrcError)


def test_capture_carries_seq() -> None:
    """まとめた記録に連番が入る."""
    lines = [io.parse_rx_line(make_line(t * INTERVAL_US, make_raw(), seq=100 + t))
             for t in range(3)]
    np.testing.assert_array_equal(io.lines_to_capture(lines).seq, [100, 101, 102])


def test_count_missing_by_seq() -> None:
    """欠番を数え、不明は除き、一周をまたいでも数えられる."""
    unk = io.SEQ_UNKNOWN
    assert io.count_missing_by_seq(np.array([10, 11, 13, unk, 17])) == 4  # 12, 14, 15, 16
    wrap = np.array([io.SEQ_MODULUS - 2, io.SEQ_MODULUS - 1, 1])
    assert io.count_missing_by_seq(wrap) == 1  # 0 が欠け
    assert io.count_missing_by_seq(np.array([unk, 5])) is None


def test_count_missing_by_seq_rejects_duplicate_and_reverse() -> None:
    """重複・逆行は対応づけの誤りとして弾く."""
    with pytest.raises(ValueError, match="重複"):
        io.count_missing_by_seq(np.array([1, 2, 2, 3]))
    with pytest.raises(ValueError, match="逆行"):
        io.count_missing_by_seq(np.array([5, 6, 4]))


# ---- format=3（送信機 2 台・1 区間だけ） -------------------------------------------

TX_S = "02000000000a"  # 主の送信機（ESP32）
TX_P = "02000000000c"  # 2 台目（Pico W）


def make_line_v3(rx_us: int, raw: np.ndarray, tx: str, section: str, seq: int,
                 sig_mode: int = 1) -> str:
    """CRC を付けた format=3 の 1 行を作る（raw は区間 1 つ分 = 128 byte）."""
    h = HEADER
    body = (f"CSI,{rx_us},{tx},{seq},{h['rssi']},{h['noise']},{h['channel']},{h['secondary']},"
            f"{sig_mode},{h['mcs']},{h['cwb']},{h['stbc']},{h['sgi']},0,{section},{raw.size},"
            f"{raw.tobytes().hex()}")
    return f"{body}*{zlib.crc32(body.encode()):08x}\n"


def test_parse_v3_reads_tx_and_section() -> None:
    """Format=3 の送信元と区間を読み、送信元はコロン区切りにそろえる."""
    raw = make_raw()
    rec = io.parse_rx_line(make_line_v3(100, raw[128:], TX_S, "htltf", seq=7))
    assert (rec.tx, rec.section, rec.seq, rec.raw.size) == ("02:00:00:00:00:0a", "htltf", 7, 128)
    bad = make_line_v3(100, raw[128:], TX_S, "xxx", seq=7)
    with pytest.raises(ValueError):
        io.parse_rx_line(bad)


def test_v3_sections_match_v2_layout() -> None:
    """Format=3 の htltf / lltf の区間から、format=2 と同じ値が取り出せる."""
    raw = make_raw()
    v2 = io.lines_to_capture([io.parse_rx_line(make_line(0, raw, seq=0))], ltf="htltf")
    v3 = io.lines_to_capture([io.parse_rx_line(make_line_v3(0, raw[128:], TX_S, "htltf", 0))],
                             ltf="htltf")
    np.testing.assert_array_equal(v2.csi, v3.csi)
    v2l = io.lines_to_capture([io.parse_rx_line(make_line(0, raw, seq=0))], ltf="lltf")
    v3l = io.lines_to_capture([io.parse_rx_line(make_line_v3(0, raw[:128], TX_P, "lltf", 0, 0))],
                              ltf="lltf")
    np.testing.assert_array_equal(v2l.csi, v3l.csi)
    assert v3l.tx == "02:00:00:00:00:0c"
    with pytest.raises(ValueError, match="lltf"):  # LLTF だけの行から HT-LTF は取れない
        io.lines_to_capture([io.parse_rx_line(make_line_v3(0, raw[:128], TX_P, "lltf", 0, 0))])


def test_load_two_transmitters_selects_primary_or_given(tmp_path: Path) -> None:
    """送信機 2 台の記録は、既定で見出しの主の送信機を読み、tx で 2 台目も読める."""
    raw = make_raw()
    lines = ["# CSI receiver format=3\n", f"# tx id=0 mac={TX_S} primary=1\n",
             f"# tx id=1 mac={TX_P}\n"]
    for i in range(10):
        lines.append(make_line_v3(i * INTERVAL_US, raw[128:], TX_S, "htltf", i))
        lines.append(make_line_v3(i * INTERVAL_US + 3000, raw[:128], TX_P, "lltf", 100 + i, 0))
    path = tmp_path / "two.csv"
    path.write_text("".join(lines), encoding="utf-8")
    s = io.load_rx_csv(path)
    assert (len(s.seq), s.tx, s.seq[0]) == (10, "02:00:00:00:00:0a", 0)
    p = io.load_rx_csv(path, ltf="lltf", tx="02:00:00:00:00:0c")
    assert (len(p.seq), p.seq[0], p.csi.shape[1]) == (10, 100, 52)
    assert io.transmitters_in(path) == {"02:00:00:00:00:0a": 10, "02:00:00:00:00:0c": 10}
    with pytest.raises(ValueError):
        io.load_rx_csv(path, tx="aa:bb:cc:dd:ee:ff")
    # 主の送信機が分からなければ、選ぶよう求める
    no_header = tmp_path / "no_header.csv"
    no_header.write_text("".join(lines[3:]), encoding="utf-8")
    with pytest.raises(ValueError, match="tx で選んで"):
        io.load_rx_csv(no_header)


def test_parse_tx_header_with_and_without_name() -> None:
    """見出しの送信機の行から MAC と機種名を読む（name= のない古い見出しは None）."""
    assert io.parse_tx_header("# tx id=1 mac=02000000000c name=PicoW\n") == (
        "02:00:00:00:00:0c", "PicoW")
    assert io.parse_tx_header("# tx id=0 mac=02000000000a primary=1") == (
        "02:00:00:00:00:0a", None)
    assert io.parse_tx_header("# stats rate_hz=99.0") is None
