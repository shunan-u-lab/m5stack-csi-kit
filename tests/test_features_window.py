"""窓ごとの特徴量のテスト."""

from __future__ import annotations

import numpy as np
import pytest

from csikit import features

N_SUBCARRIERS = 56
N_STREAMS = 1
WINDOW = 100  # 100 Hz で 1 秒


def test_normalize_per_packet_removes_common_gain() -> None:
    """パケットごとに倍率が違っても、正規化後は同じ形になる."""
    shape = np.linspace(0.5, 1.5, N_SUBCARRIERS).reshape(1, N_SUBCARRIERS, 1)
    gain = np.array([1.0, 3.0, 10.0]).reshape(3, 1, 1)
    norm = features.normalize_per_packet(gain * shape)
    np.testing.assert_allclose(norm[0], norm[1])
    np.testing.assert_allclose(norm[2], norm[0])
    np.testing.assert_allclose(norm.mean(axis=1), 1.0)


def test_normalize_per_packet_rejects_zero_packet() -> None:
    """全サブキャリアが 0 のパケットは弾く."""
    amp = np.ones((2, N_SUBCARRIERS, N_STREAMS))
    amp[1] = 0.0
    with pytest.raises(ValueError):
        features.normalize_per_packet(amp)


def test_windowed_temporal_std_separates_still_and_moving() -> None:
    """一定の窓は 0、振動する窓は振幅に応じた値、端数は捨てる."""
    still = np.ones((WINDOW, N_SUBCARRIERS, N_STREAMS))
    moving = 1.0 + 0.1 * np.sign(np.sin(np.arange(WINDOW) + 0.5)).reshape(WINDOW, 1, 1)
    moving = np.broadcast_to(moving, (WINDOW, N_SUBCARRIERS, N_STREAMS))
    extra = np.ones((WINDOW // 2, N_SUBCARRIERS, N_STREAMS))
    out = features.windowed_temporal_std(np.concatenate([still, moving, extra]), WINDOW)
    assert out.shape == (2,)
    assert out[0] == pytest.approx(0.0)
    assert out[1] == pytest.approx(0.1, abs=0.01)  # ±0.1 の矩形波の標準偏差


def test_windowed_temporal_std_validates_input() -> None:
    """窓が短すぎる・データが足りない場合は弾く."""
    amp = np.ones((10, N_SUBCARRIERS, N_STREAMS))
    with pytest.raises(ValueError):
        features.windowed_temporal_std(amp, 1)
    with pytest.raises(ValueError):
        features.windowed_temporal_std(amp, 11)


def test_sliding_temporal_std_matches_windowed() -> None:
    """窓の倍数の位置では windowed_temporal_std と一致し、長さは packets - window + 1."""
    rng = np.random.default_rng(0)
    amp = 40.0 + rng.normal(0.0, 2.0, (3 * WINDOW + 7, N_SUBCARRIERS, 2))
    sliding = features.sliding_temporal_std(amp, WINDOW)
    assert sliding.shape == (len(amp) - WINDOW + 1,)
    windowed = features.windowed_temporal_std(amp, WINDOW)
    np.testing.assert_allclose(sliding[::WINDOW][: len(windowed)], windowed, rtol=1e-9)


def test_sliding_temporal_std_validates_input() -> None:
    """窓が短すぎる・データが足りない・NaN を弾く."""
    amp = np.ones((10, N_SUBCARRIERS, N_STREAMS))
    with pytest.raises(ValueError):
        features.sliding_temporal_std(amp, 1)
    with pytest.raises(ValueError):
        features.sliding_temporal_std(amp, 11)
    amp[0, 0, 0] = np.nan
    with pytest.raises(ValueError):
        features.sliding_temporal_std(amp, 5)
