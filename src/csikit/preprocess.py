"""CSI 前処理の雛形.

生CSIから「振幅」と「位相」を取り出し、基本的なノイズ除去・位相補正を行う。
位相は CFO/SFO/PBD などハードウェア由来のオフセットを含むため、そのままでは使えない。
ここでは学習用に最も基本的な「線形位相除去（サブキャリア方向の1次項除去）」を実装する。
詳細は docs/02-mechanisms/signal-processing.md を参照。
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from csikit.io import AXIS_SUBCARRIER, validate_csi


def amplitude(csi: NDArray[np.complex128]) -> NDArray[np.float64]:
    """複素CSIから振幅 |H| を取り出す.

    Args:
        csi: 形状 (packets, subcarriers, streams) の複素CSI配列。

    Returns:
        同形状の実数振幅配列（単位なし、線形スケール）。
    """
    validate_csi(csi)
    return np.abs(csi).astype(np.float64)


def phase(csi: NDArray[np.complex128]) -> NDArray[np.float64]:
    """複素CSIから巻き戻し済み位相を取り出す.

    Args:
        csi: 形状 (packets, subcarriers, streams) の複素CSI配列。

    Returns:
        サブキャリア方向に unwrap した位相配列（単位: ラジアン phase_rad）。
    """
    validate_csi(csi)
    raw_phase_rad = np.angle(csi)
    return np.unwrap(raw_phase_rad, axis=AXIS_SUBCARRIER).astype(np.float64)


def sanitize_phase(csi: NDArray[np.complex128]) -> NDArray[np.float64]:
    """サブキャリア方向の線形位相（傾き・オフセット）を除去する.

    CFO/SFO に由来するサブキャリア依存の線形位相を、最小二乗で推定した
    1次成分を引くことで除去する簡易版（いわゆる linear transform 法）。
    校正の入門用であり、厳密な較正は基準アンテナ・基準パケットを要する。

    Args:
        csi: 形状 (packets, subcarriers, streams) の複素CSI配列。

    Returns:
        線形位相除去後の位相配列（単位: ラジアン phase_rad）。
    """
    unwrapped_rad = phase(csi)
    n_subcarriers = unwrapped_rad.shape[AXIS_SUBCARRIER]
    # サブキャリアインデックス（0..N-1）を周波数軸の代理として使う
    subcarrier_idx = np.arange(n_subcarriers, dtype=np.float64)

    # サブキャリア軸を末尾に移動して (..., N) の形にし、まとめて最小二乗
    moved = np.moveaxis(unwrapped_rad, AXIS_SUBCARRIER, -1)
    flat = moved.reshape(-1, n_subcarriers)

    # 各行 y = a*x + b の (a, b) を一括推定
    design = np.vstack([subcarrier_idx, np.ones_like(subcarrier_idx)]).T  # (N, 2)
    coeffs, *_ = np.linalg.lstsq(design, flat.T, rcond=None)  # (2, rows)
    linear_component = design @ coeffs  # (N, rows)
    sanitized_flat = flat - linear_component.T

    sanitized = sanitized_flat.reshape(moved.shape)
    return np.moveaxis(sanitized, -1, AXIS_SUBCARRIER)


def hampel_denoise(
    amp: NDArray[np.float64],
    window: int = 5,
    n_sigmas: float = 3.0,
) -> NDArray[np.float64]:
    """振幅時系列の外れ値を Hampel フィルタで平滑化する.

    CSI 振幅にはバースト的なスパイクが乗ることがある。各パケットを中央値からの
    乖離で評価し、しきい値を超えたら局所中央値で置換する。

    Args:
        amp: 形状 (packets, subcarriers, streams) の振幅配列。
        window: 片側ではなく全体の窓長（パケット数）。奇数を想定。
        n_sigmas: 外れ値判定のしきい（中央絶対偏差の何倍か）。

    Returns:
        同形状の平滑化済み振幅配列。

    Raises:
        ValueError: window が 1 未満の場合。
    """
    if window < 1:
        raise ValueError(f"window は 1 以上が必要です: {window}")
    # 正規分布で MAD を標準偏差に変換する定数
    mad_to_sigma = 1.4826
    half = window // 2
    out: NDArray[np.float64] = amp.copy()
    n_packets = amp.shape[0]
    for i in range(n_packets):
        lo = max(0, i - half)
        hi = min(n_packets, i + half + 1)
        local = amp[lo:hi]
        med = np.median(local, axis=0)
        mad = np.median(np.abs(local - med), axis=0)
        sigma = mad_to_sigma * mad
        deviation = np.abs(amp[i] - med)
        mask = deviation > (n_sigmas * sigma)
        out[i] = np.where(mask, med, amp[i])
    return out
