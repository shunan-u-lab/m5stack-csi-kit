"""PCA 特徴のテスト."""

from __future__ import annotations

import numpy as np
import pytest

from csikit import features

N_PACKETS = 500
N_SUBCARRIERS = 56
N_STREAMS = 1
SEED = 0
NOISE_STD = 0.01  # 線形振幅
SIGNAL_AMP = 1.0  # 線形振幅


def _make_rank1_signal() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """全サブキャリアが同じ時系列で揺れる（形は固定）信号 + 小さい雑音を作る."""
    rng = np.random.default_rng(SEED)
    t = np.arange(N_PACKETS)
    wave = SIGNAL_AMP * np.sin(2 * np.pi * t / 50)  # 50 パケット周期
    pattern = np.linspace(1.0, 2.0, N_SUBCARRIERS)
    pattern /= np.linalg.norm(pattern)
    base = 10.0
    amp = base + wave[:, None] * pattern[None, :]
    amp = amp + rng.normal(0.0, NOISE_STD, amp.shape)
    return amp.reshape(N_PACKETS, N_SUBCARRIERS, N_STREAMS), wave, pattern


def test_pca_recovers_dominant_pattern() -> None:
    """全サブキャリア共通の変動が第 1 成分に集まり、主軸とスコアが復元される."""
    amp, wave, pattern = _make_rank1_signal()
    res = features.pca_subcarriers(amp, 3)
    assert res.scores.shape == (N_PACKETS, 3)
    assert res.components.shape == (3, N_SUBCARRIERS * N_STREAMS)
    assert res.explained_variance_ratio[0] > 0.9
    assert np.all(np.diff(res.explained_variance_ratio) <= 0)  # 降順
    np.testing.assert_allclose(res.components[0], pattern, atol=0.05)  # 符号も揃う
    assert abs(np.corrcoef(res.scores[:, 0], wave)[0, 1]) > 0.99
    np.testing.assert_allclose(np.linalg.norm(res.components, axis=1), 1.0)


def test_pca_scores_reconstruct_centered_data() -> None:
    """全成分を使えばスコア × 主軸 + 平均で元データに戻る."""
    rng = np.random.default_rng(SEED)
    amp = rng.normal(5.0, 1.0, (20, 4, 2))
    res = features.pca_subcarriers(amp, 8)
    recon = res.scores @ res.components + res.mean
    np.testing.assert_allclose(recon, amp.reshape(20, -1), atol=1e-10)
    assert res.explained_variance_ratio.sum() == pytest.approx(1.0)


def test_pca_validates_input() -> None:
    """次元・NaN・成分数・無変動を弾く."""
    amp = np.random.default_rng(SEED).normal(size=(10, N_SUBCARRIERS, N_STREAMS))
    with pytest.raises(ValueError):
        features.pca_subcarriers(amp[:, :, 0], 1)
    with pytest.raises(ValueError):
        features.pca_subcarriers(amp, 0)
    with pytest.raises(ValueError):
        features.pca_subcarriers(amp, 11)  # min(10, 56) を超える
    bad = amp.copy()
    bad[0, 0, 0] = np.nan
    with pytest.raises(ValueError):
        features.pca_subcarriers(bad, 1)
    with pytest.raises(ValueError):
        features.pca_subcarriers(np.ones((10, N_SUBCARRIERS, N_STREAMS)), 1)
