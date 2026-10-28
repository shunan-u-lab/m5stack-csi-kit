r"""人が動くと CSI の「形」が変わる様子を、実機なしで描く（チュートリアル 1 章の 🌳）.

    uv run python examples/tutorial/multipath_demo.py                 # 画面に出す
    uv run python examples/tutorial/multipath_demo.py --save demo.png # ファイルに保存

送信機から受信機へ、電波が 3 本の道で届くとする（**仮定**。本物の部屋では道はもっと多い）:

- 道 0: まっすぐ届く道（長さ ``DIRECT_M``）
- 道 1: 壁で何度か跳ね返って届く長い道（長さ ``WALL_M``・動かない）
- 道 2: 人の体で跳ね返って届く道（長さ ``PERSON_M`` + 人の動きの分）

受信機で測れる CSI は、各サブキャリアの周波数 f で、道ごとの波を足し合わせたもの:

    H(f) = Σ_i a_i · exp(-j · 2π · f · d_i / c)

（a_i は道の弱まり方、d_i は道の長さ、c は光の速さ）。人が動いて d_2 が数 cm 変わるだけで、
足し合わせの強め合い・打ち消し合いが変わり、56 本の振幅 |H(f)| の形が変わる。
56 本の間に凸凹ができるのは、長さの差が大きい道があるから
（帯域 17.5 MHz の中で凸凹が 1 周期できるには、差が約 17 m 要る）。

失敗時の挙動: 引数が負の値などなら argparse がエラーを出して終わる。
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

import numpy as np
from numpy.typing import NDArray

LIGHT_SPEED_M_S = 299_792_458.0  # 光の速さ [m/s]
CENTER_HZ = 2_462e6  # ch11 の中心周波数 [Hz]（このキットの既定のチャンネル）
SPACING_HZ = 312.5e3  # サブキャリアの間隔 [Hz]（20 MHz / 64）
SUBCARRIERS = np.array([k for k in range(-28, 29) if k != 0])  # HT-LTF で使える 56 本の番号
DIRECT_M = 3.0  # まっすぐ届く道の長さ [m]（仮定: 送受の距離 3 m）
WALL_M = 15.0  # 壁で跳ね返る長い道の長さ [m]（仮定）
WALL_GAIN = 0.5  # その強さ（まっすぐ届く波を 1 としたとき。仮定）
PERSON_M = 4.2  # 人で跳ね返る道の長さ [m]（仮定）
PERSON_GAIN = 0.4  # その強さ（仮定）
MOVES_CM = (0.0, 1.5, 3.0, 4.5, 6.0)  # 人で跳ね返る道の長さが変わる量 [cm]


def subcarrier_freqs_hz(center_hz: float = CENTER_HZ) -> NDArray[np.float64]:
    """56 本のサブキャリアの周波数 [Hz]."""
    freqs: NDArray[np.float64] = center_hz + SUBCARRIERS * SPACING_HZ
    return freqs


def channel(freqs_hz: NDArray[np.float64], lengths_m: Sequence[float],
            gains: Sequence[float]) -> NDArray[np.complex128]:
    """道ごとの波を足し合わせた CSI H(f).

    Args:
        freqs_hz: サブキャリアの周波数 [Hz]。
        lengths_m: 道の長さ [m]。
        gains: 道の強さ（まっすぐの道を 1 とした比）。

    Returns:
        各サブキャリアの複素数 H(f)（絶対値 = 振幅、角度 = 位相）。
    """
    h = np.zeros(freqs_hz.shape, dtype=np.complex128)
    for d, a in zip(lengths_m, gains, strict=True):
        h += a * np.exp(-2j * np.pi * freqs_hz * d / LIGHT_SPEED_M_S)
    return h


def amplitudes_for_moves(moves_cm: Sequence[float] = MOVES_CM) -> NDArray[np.float64]:
    """人で跳ね返る道の長さを変えたときの振幅 |H(f)|（形状: 動きの数 × 56）."""
    f = subcarrier_freqs_hz()
    gains = [1.0, WALL_GAIN, PERSON_GAIN]
    rows = [np.abs(channel(f, [DIRECT_M, WALL_M, PERSON_M + m / 100], gains))
            for m in moves_cm]
    return np.stack(rows)


def main(argv: list[str] | None = None) -> int:
    """図を描く."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--save", help="画面に出さず、この名前で保存する（例: demo.png）")
    args = parser.parse_args(argv)

    import matplotlib

    if args.save:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    amps = amplitudes_for_moves()
    fig, ax = plt.subplots(figsize=(8, 4))
    for move, amp in zip(MOVES_CM, amps, strict=True):
        ax.plot(SUBCARRIERS, amp, marker=".", label=f"+{move:.1f} cm")
    ax.set_xlabel("subcarrier index")
    ax.set_ylabel("|H(f)|  (amplitude)")
    ax.set_title("person moves a few cm -> CSI shape changes (3-path model)")
    ax.legend(title="path change")
    fig.tight_layout()
    if args.save:
        fig.savefig(args.save)
        print(args.save)
    else:
        plt.show()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
