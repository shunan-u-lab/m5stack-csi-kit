r"""受信機が PC に送る CSI の 1 行を、実機なしで作って読む（チュートリアル 4・5 章の 🌳）.

    uv run python examples/tutorial/csi_line_demo.py           # 1 行を作って読む
    uv run python examples/tutorial/csi_line_demo.py --break   # 1 文字壊して CRC で気づく

受信機の 1 行（format=3）は、カンマで区切った見出しと、CSI の生データ（16 進）と、
最後に ``*`` に続く CRC（壊れていないかを確かめる 8 桁の数）でできている:

    CSI,<受信時刻 µs>,<送信元 MAC>,<連番>,<RSSI>,<雑音>,<ch>,…,<区間>,<長さ>,<16 進>*<CRC>

16 進の部分は 2 バイトで 1 サブキャリア（[虚部, 実部] の順の int8）。HT-LTF は 64 サブキャリア分
（128 バイト）で、そのうち使えるのは 56 本（両端と真ん中は使わない）。ここで作る値は
**練習用の作り物**（本物の部屋の CSI ではない）。
送信元の MAC はダミー（``02:00:00:00:00:01``）。

失敗時の挙動: 引数が違えば argparse がエラーを出して終わる。``--break`` のときは、CRC の不一致を
``csikit.io.RxCrcError`` として受け止めて表示し、終了コード 0 で終わる
（壊れた行を捨てる様子を見せるため）。
"""

from __future__ import annotations

import argparse
import zlib
from collections.abc import Sequence

import numpy as np

from csikit import io

DUMMY_TX = "020000000001"  # 送信元の MAC（ダミー）
SEQ = 1234  # 送信機の連番
RX_US = 5_000_000  # 受信時刻 [µs]（受信機が起動してからの時間）
RSSI_DBM = -45  # 受けた強さ [dBm]
NOISE_DBM = -96  # 雑音の強さ [dBm]
CHANNEL = 11
N_SUBCARRIERS = 64  # HT-LTF の区間に入っているサブキャリアの数（使えるのは 56）
SECTION_BYTES = N_SUBCARRIERS * 2  # 1 サブキャリア = 2 バイト（虚部・実部）
IMAG = 3  # 練習用の値: 全サブキャリアの虚部
REAL = 4  # 練習用の値: 全サブキャリアの実部（|3 + 4j| = 5 になる）


def make_line() -> str:
    """練習用の CSI 行（format=3・HT-LTF）を作る."""
    raw = bytes([IMAG, REAL] * N_SUBCARRIERS).hex()
    # 見出しの並び: rx_us, tx, seq, rssi, noise, ch, secondary, sig_mode, mcs, cwb, stbc, sgi,
    #              first_word_invalid, 区間, 長さ
    body = (f"CSI,{RX_US},{DUMMY_TX},{SEQ},{RSSI_DBM},{NOISE_DBM},{CHANNEL},0,1,0,0,0,0,0,"
            f"htltf,{SECTION_BYTES},{raw}")
    return f"{body}*{zlib.crc32(body.encode()):08x}"


def break_one_char(line: str) -> str:
    """連番の数字を 1 文字だけ変える（USB の転送で 1 文字化けた、という想定）."""
    return line.replace(f",{SEQ},", f",{SEQ + 1},", 1)


def describe(line: str) -> list[str]:
    """1 行を読み、分かったことを文にする."""
    rec = io.parse_rx_line(line)
    cap = io.lines_to_capture([rec])
    amp = np.abs(cap.csi[0, :, 0])
    valid = amp[~np.isnan(amp)]
    return [
        f"連番 {rec.seq}・RSSI {rec.rssi_dbm} dBm・チャンネル {rec.channel}・送信元 {rec.tx}",
        f"使えるサブキャリア {valid.size} 本・振幅 {valid[0]:.1f}（全部同じ値の練習用データ）",
    ]


def main(argv: Sequence[str] | None = None) -> int:
    """1 行を作って読む（``--break`` なら壊してから読む）."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--break", dest="broken", action="store_true",
                        help="1 文字壊して、CRC で気づく様子を見る")
    args = parser.parse_args(argv)
    line = make_line()
    print(line[:70] + " …（16 進が続く）… " + line[-9:])
    if args.broken:
        line = break_one_char(line)
        print("連番の 1 文字を変えました")
    try:
        for text in describe(line):
            print(text)
    except io.RxCrcError as e:
        print(f"読めません: {e} → この行は捨てて、bad_crc に数えます")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
