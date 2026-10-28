"""ロックイン検出（features.dft_amplitude / features.lockin）のテスト."""

from __future__ import annotations

import numpy as np
import pytest

from csikit import features

SEED = 0
RATE_HZ = 100.0  # 受信レート
DURATION_SEC = 30.0
F_MOD_HZ = 1.0  # 反射体を回す周波数
SIGNAL_AMP = 0.01  # 正規化振幅
NOISE_STD = 0.005
N_CHANNELS = 8
SIGNAL_CHANNEL = 3


def jittered_times(rng: np.random.Generator) -> np.ndarray:
    """100 Hz に ±2 ms のばらつきと 2% の欠番を入れた受信時刻 [sec]."""
    t = np.arange(0.0, DURATION_SEC, 1.0 / RATE_HZ)
    t = t + rng.uniform(-0.002, 0.002, t.size)
    keep = rng.random(t.size) > 0.02
    return np.sort(t[keep])


def test_dft_amplitude_recovers_sine_amplitude_with_irregular_sampling() -> None:
    """不等間隔・欠番ありでも、正弦波の振幅がほぼそのまま出る."""
    rng = np.random.default_rng(SEED)
    t = jittered_times(rng)
    x = (0.5 + SIGNAL_AMP * np.sin(2 * np.pi * F_MOD_HZ * t + 0.3))[:, None]
    amp = features.dft_amplitude(t, x, np.array([F_MOD_HZ, 1.7]))
    assert amp[0, 0] == pytest.approx(SIGNAL_AMP, rel=0.05)
    assert amp[1, 0] < SIGNAL_AMP * 0.05  # 別の周波数にはほぼ出ない


def test_lockin_finds_modulated_channel_and_tolerates_frequency_error() -> None:
    """揺れているチャンネルだけ SN 比が高く、周波数が少しずれていても拾う."""
    rng = np.random.default_rng(SEED)
    t = jittered_times(rng)
    x = 1.0 + rng.normal(0.0, NOISE_STD, (t.size, N_CHANNELS))
    true_f = F_MOD_HZ + 0.015  # 駆動のずれ
    x[:, SIGNAL_CHANNEL] += SIGNAL_AMP * np.cos(2 * np.pi * true_f * t)
    res = features.lockin(t, x, F_MOD_HZ)
    assert res.freq_hz == pytest.approx(true_f, abs=0.01)
    assert res.snr_db[SIGNAL_CHANNEL] > 20.0
    others = np.delete(res.snr_db, SIGNAL_CHANNEL)
    assert np.all(others < 10.0)
    assert res.amplitude[SIGNAL_CHANNEL] == pytest.approx(SIGNAL_AMP, rel=0.1)


def test_lockin_validates_input() -> None:
    """幅の大小関係・周波数・時刻の不正を弾く."""
    t = np.linspace(0.0, 10.0, 1001)
    x = np.zeros((t.size, 2))
    with pytest.raises(ValueError):
        features.lockin(t, x, F_MOD_HZ, search_hz=0.2, guard_hz=0.1)
    with pytest.raises(ValueError):
        features.lockin(t, x, 0.3, band_hz=0.4)
    with pytest.raises(ValueError):
        features.dft_amplitude(t[::-1], x, np.array([1.0]))
    with pytest.raises(ValueError):
        features.dft_amplitude(t, x[:-1], np.array([1.0]))
