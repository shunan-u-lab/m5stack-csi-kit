"""前処理・特徴抽出・IOの基本動作テスト.

合成CSI（既知の振幅・位相）を作り、各関数が期待どおりに振る舞うかを検証する。
"""

from __future__ import annotations

import numpy as np
import pytest

from csikit import features, io, preprocess

# 合成データの形状（パケット, サブキャリア, ストリーム）
N_PACKETS = 64
N_SUBCARRIERS = 30  # Intel 5300 を模した値
N_STREAMS = 2


def make_synthetic_csi() -> np.ndarray:
    """既知の振幅2.0・線形位相を持つ合成CSIを生成する."""
    amp = np.full((N_PACKETS, N_SUBCARRIERS, N_STREAMS), 2.0)
    subcarrier_idx = np.arange(N_SUBCARRIERS).reshape(1, N_SUBCARRIERS, 1)
    phase_rad = 0.1 * subcarrier_idx + 0.3  # 傾き0.1, 切片0.3 の線形位相
    return (amp * np.exp(1j * phase_rad)).astype(np.complex128)


def test_validate_rejects_real_array() -> None:
    """実数配列は TypeError になる."""
    with pytest.raises(TypeError):
        io.validate_csi(np.ones((N_PACKETS, N_SUBCARRIERS, N_STREAMS)))


def test_amplitude_recovers_magnitude() -> None:
    """合成CSIの振幅が 2.0 に一致する."""
    csi = make_synthetic_csi()
    amp = preprocess.amplitude(csi)
    np.testing.assert_allclose(amp, 2.0, atol=1e-9)


def test_sanitize_phase_removes_linear_term() -> None:
    """線形位相除去後はほぼ 0 になる（傾き・切片が消える）."""
    csi = make_synthetic_csi()
    sanitized = preprocess.sanitize_phase(csi)
    np.testing.assert_allclose(sanitized, 0.0, atol=1e-6)


def test_hampel_replaces_spike() -> None:
    """挿入したスパイクが平滑化で抑制される."""
    csi = make_synthetic_csi()
    amp = preprocess.amplitude(csi)
    amp[10, 0, 0] = 100.0  # 明確な外れ値
    denoised = preprocess.hampel_denoise(amp)
    assert denoised[10, 0, 0] < 10.0


def test_motion_indicator_length() -> None:
    """動き指標の長さは packets-1."""
    csi = make_synthetic_csi()
    amp = preprocess.amplitude(csi)
    motion = features.motion_indicator(amp)
    assert motion.shape == (N_PACKETS - 1,)


def test_save_load_roundtrip(tmp_path) -> None:
    """保存→読込で配列が一致する."""
    csi = make_synthetic_csi()
    path = tmp_path / "csi.npz"
    io.save_npz(path, csi)
    loaded = io.load_npz(path)
    np.testing.assert_allclose(loaded, csi)
