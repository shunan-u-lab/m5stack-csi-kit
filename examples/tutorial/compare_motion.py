r"""静かなときと手を振ったときの CSI を比べ、「動きの大きさ」を 1 秒ごとの数にする（6 章）.

    uv run python examples/tutorial/compare_motion.py --demo        # 練習用データ（実機なし）
    uv run python examples/tutorial/compare_motion.py data/raw/<…>-static.csv data/raw/<…>-wave.csv
    uv run python examples/tutorial/compare_motion.py --demo --save motion.png

動きの大きさ（この例での決め方。研究で決まった唯一の方法ではない）:

1. 各パケットの 56 本の振幅 |H(f)| を、そのパケットの平均で割る
   （**形**だけを残す。全体の強さの揺れを除く）
2. 1 秒ごとに、各サブキャリアの値が時間とともにどれだけ揺れたか（標準偏差）を求め、56 本で平均する

何も動いていなければ雑音の分だけの小さな値、人が動くと形が揺れて大きな値になる。

``--demo`` の練習用データは、1 章の ``multipath_demo.py`` と同じ 3 本の道のモデル（**仮定**）に
雑音を足して作る。手を振るときは、人で跳ね返る道の長さが 1 秒に 1 往復、
±``WAVE_AMPLITUDE_M`` だけ変わるとする（**仮定**）。雑音の大きさは、2026-10-28 に実機で取った
静かなときの記録（動きの大きさ 平均 0.021）に近くなるように決めた。

失敗時の挙動: 記録を読めなければ ``csikit.io`` の例外（ValueError など）で終わる。
記録が 1 秒より短ければ ValueError。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

sys.path.insert(0, str(Path(__file__).resolve().parent))  # 同じフォルダの multipath_demo を読む
import multipath_demo  # noqa: E402

from csikit import io  # noqa: E402

WINDOW_SEC = 1.0  # 動きの大きさを求める区切り [sec]
RATE_HZ = 100  # 練習用データのパケットの数 [Hz]（実機の送信機と同じ）
DEMO_SECONDS = 30  # 練習用データの長さ [sec]
NOISE_STD = 0.019  # 練習用データの雑音（振幅を平均で割った値に対して。実機の静かなときに合わせた）
WAVE_AMPLITUDE_M = 0.05  # 手を振ったときの、人で跳ね返る道の長さの変化 [m]（仮定）
WAVE_HZ = 1.0  # 手を振る速さ [往復/sec]（仮定）
SEED = 0  # 練習用データの乱数の種（毎回同じ結果にする）


def shape_of(amplitude: NDArray[np.float64]) -> NDArray[np.float64]:
    """振幅（パケット × サブキャリア）を、パケットごとの平均で割って「形」にする."""
    return amplitude / amplitude.mean(axis=1, keepdims=True)


def motion_index(shape: NDArray[np.float64], time_sec: NDArray[np.float64],
                 window_sec: float = WINDOW_SEC) -> NDArray[np.float64]:
    """区切りごとの動きの大きさ（各サブキャリアの標準偏差を 56 本で平均）.

    Raises:
        ValueError: 区切り 1 つ分より短い。
    """
    n_windows = int((time_sec[-1] - time_sec[0]) // window_sec)
    if n_windows < 1:
        raise ValueError(f"記録が {window_sec:g} 秒より短いので比べられません")
    out = []
    for k in range(n_windows):
        start = time_sec[0] + k * window_sec
        rows = (time_sec >= start) & (time_sec < start + window_sec)
        out.append(float(shape[rows].std(axis=0).mean()))
    return np.array(out)


def load_shape(path: Path) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """記録を読み、使えるサブキャリアの「形」と時刻 [sec] を返す."""
    cap = io.load_rx_csv(path)
    amplitude = np.abs(cap.csi[:, :, 0])
    usable = ~np.isnan(amplitude).any(axis=0)  # 両端・真ん中の使わないサブキャリアを除く
    return shape_of(amplitude[:, usable]), cap.time_sec


def demo_shape(kind: str, seconds: int = DEMO_SECONDS,
               seed: int = SEED) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """練習用データ（``static`` か ``wave``）の「形」と時刻を作る."""
    rng = np.random.default_rng(seed + (kind == "wave"))
    t = np.arange(seconds * RATE_HZ) / RATE_HZ
    freqs = multipath_demo.subcarrier_freqs_hz()
    gains = (1.0, multipath_demo.WALL_GAIN, multipath_demo.PERSON_GAIN)
    rows = []
    for ti in t:
        move = WAVE_AMPLITUDE_M * np.sin(2 * np.pi * WAVE_HZ * ti) if kind == "wave" else 0.0
        lengths = (multipath_demo.DIRECT_M, multipath_demo.WALL_M, multipath_demo.PERSON_M + move)
        rows.append(np.abs(multipath_demo.channel(freqs, lengths, gains)))
    shape = shape_of(np.array(rows))
    return shape * (1 + rng.normal(0.0, NOISE_STD, shape.shape)), t


def summarize(label: str, index: NDArray[np.float64]) -> str:
    """1 つの記録の結果の行."""
    return (f"{label:<8} 動きの大きさ（1 秒ごと）: 平均 {index.mean():.3f}  最大 {index.max():.3f}"
            f"  （{index.size} 秒分）")


def main(argv: Sequence[str] | None = None) -> int:
    """2 つの記録（か練習用データ）を比べる."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("csv", nargs="*", type=Path,
                        help="比べる記録（静かなとき・手を振ったときの順）")
    parser.add_argument("--demo", action="store_true", help="練習用データで比べる（実機なし）")
    parser.add_argument("--save", help="図をファイルに保存する（例: motion.png）")
    args = parser.parse_args(argv)
    if args.demo == bool(args.csv):
        parser.error("--demo か、記録を 2 つ指定してください")
    if args.csv and len(args.csv) != 2:
        parser.error("記録は 2 つ（静かなとき・手を振ったとき）を指定してください")
    if args.demo:
        series = {kind: motion_index(*demo_shape(kind)) for kind in ("static", "wave")}
    else:
        series = {io.recording_name(p).split("-", 2)[-1]: motion_index(*load_shape(p))
                  for p in args.csv}
    for label, index in series.items():
        print(summarize(label, index))
    (first, a), (second, b) = series.items()
    print(f"{second} の平均は {first} の {b.mean() / a.mean():.1f} 倍")
    if args.save:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(8, 3.5))
        for label, index in series.items():
            ax.plot(np.arange(index.size) * WINDOW_SEC, index, marker="o", label=label)
        ax.set_xlabel("time [s]")
        ax.set_ylabel("motion index")
        ax.legend()
        fig.tight_layout()
        fig.savefig(args.save)
        print(f"図を保存しました: {args.save}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
