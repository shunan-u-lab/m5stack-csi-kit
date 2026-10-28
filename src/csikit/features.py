"""CSI からの特徴抽出雛形.

物体検知・在不在判定では、振幅の時間的なばらつき（分散・尖度）や、サブキャリア間の
相関がよく使われる。ここでは入門用に統計特徴を実装する。
詳細は docs/02-mechanisms/signal-processing.md と docs/04-applications/ を参照。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from csikit.io import AXIS_PACKET, AXIS_SUBCARRIER


def amplitude_statistics(amp: NDArray[np.float64]) -> dict[str, NDArray[np.float64]]:
    """振幅時系列のサブキャリアごとの基本統計量を計算する.

    在不在判定では、人や物体の動きにより振幅の分散が増大することを利用する。

    Args:
        amp: 形状 (packets, subcarriers, streams) の振幅配列。

    Returns:
        "mean" / "std" / "var" / "range" をキーに持つ辞書。各値は
        形状 (subcarriers, streams) の配列（パケット軸で集約済み）。
    """
    return {
        "mean": np.mean(amp, axis=AXIS_PACKET),
        "std": np.std(amp, axis=AXIS_PACKET),
        "var": np.var(amp, axis=AXIS_PACKET),
        "range": np.ptp(amp, axis=AXIS_PACKET),
    }


def motion_indicator(amp: NDArray[np.float64]) -> NDArray[np.float64]:
    """各パケット時点の「動きの強さ」を簡易指標化する.

    隣接パケット間の振幅差の二乗和（サブキャリア・ストリームで集約）を返す。
    値が大きいほど環境の変化（=動き）が大きいと解釈する。

    Args:
        amp: 形状 (packets, subcarriers, streams) の振幅配列。

    Returns:
        形状 (packets-1,) の動き指標（単位なし、相対値）。
    """
    diff = np.diff(amp, axis=AXIS_PACKET)
    # サブキャリア・ストリーム軸を二乗和で集約
    motion: NDArray[np.float64] = np.sum(diff**2, axis=(1, 2)).astype(np.float64)
    return motion


def normalize_per_packet(amp: NDArray[np.float64]) -> NDArray[np.float64]:
    """各パケットの振幅を、そのパケットのサブキャリア平均で割る.

    ESP32 の CSI は自動スケール（``manu_scale=false``）や AGC により、パケットごとに
    全体の倍率が変わりうる。共通の倍率を打ち消し、サブキャリア間の「形」の変化だけを残す。
    2026-09-23 の実測では、RSSI が 3 dB 違う 2 本の記録で生の振幅の平均が一致しており
    （41.8 と 41.8）、生の振幅の大きさは受信電力を反映していなかった。

    Args:
        amp: 形状 (packets, subcarriers, streams) の振幅配列（NaN は平均から除く）。

    Returns:
        同じ形状の正規化振幅（単位なし、サブキャリア平均が 1）。

    Raises:
        ValueError: サブキャリア平均が正でないパケットがある場合。
    """
    mean = np.nanmean(amp, axis=AXIS_SUBCARRIER, keepdims=True)
    if not np.all(mean > 0):
        raise ValueError(
            "サブキャリア平均が 0 以下のパケットがあります（全サブキャリアが 0 か NaN）"
        )
    normalized: NDArray[np.float64] = (amp / mean).astype(np.float64)
    return normalized


def windowed_temporal_std(amp: NDArray[np.float64], window: int) -> NDArray[np.float64]:
    """重ならない窓ごとに、時間方向の標準偏差をサブキャリア・ストリームで平均する.

    静止時は小さく、人が動くと大きくなる「動きの強さ」の指標。隣接パケットの差
    （``motion_indicator``）より雑音に強い。端数のパケットは捨てる。

    Args:
        amp: 形状 (packets, subcarriers, streams) の振幅配列。
        window: 窓の長さ [packets]（100 Hz なら 100 で 1 秒）。

    Returns:
        形状 (packets // window,) の指標（``amp`` と同じ単位）。

    Raises:
        ValueError: ``window`` が 2 未満、またはパケット数が ``window`` 未満の場合。
    """
    min_window = 2  # 標準偏差には 2 点以上が要る
    if window < min_window:
        raise ValueError(f"window は {min_window} 以上: {window}")
    n_windows = amp.shape[AXIS_PACKET] // window
    if n_windows == 0:
        raise ValueError(f"パケット数 {amp.shape[AXIS_PACKET]} が window={window} 未満です")
    trimmed = amp[: n_windows * window].reshape(n_windows, window, *amp.shape[1:])
    per_window: NDArray[np.float64] = np.nanmean(np.nanstd(trimmed, axis=1), axis=(1, 2))
    return per_window


@dataclass(frozen=True)
class PcaResult:
    """``pca_subcarriers`` の結果.

    特徴量の次元 ``n_features`` は subcarriers × streams（C 順に平坦化）。

    Attributes:
        scores: 形状 (packets, n_components) の主成分スコア（入力と同じ単位）。
            列 i が第 i+1 主成分の時系列。
        components: 形状 (n_components, n_features) の主軸（各行が単位ベクトル）。
            どのサブキャリアがその成分に効いているかを表す。
        explained_variance_ratio: 形状 (n_components,) の寄与率（全分散に対する比、単位なし）。
        mean: 形状 (n_features,) の時間平均（差し引いた静的成分）。
    """

    scores: NDArray[np.float64]
    components: NDArray[np.float64]
    explained_variance_ratio: NDArray[np.float64]
    mean: NDArray[np.float64]


def pca_subcarriers(amp: NDArray[np.float64], n_components: int) -> PcaResult:
    """サブキャリア（×ストリーム）方向に PCA をかけ、相関した変動を少数の成分にまとめる.

    人の動きは多くのサブキャリアを同時に揺らすため、主成分に集まりやすい。時間平均を
    差し引いてから SVD で分解する（標準化はしない。振幅の大きいサブキャリアほど重みが大きい）。
    SVD の符号は不定なので、各主軸で絶対値最大の係数が正になるよう揃えて結果を決定的にする。

    Args:
        amp: 形状 (packets, subcarriers, streams) の振幅配列（NaN 不可）。
        n_components: 取り出す主成分の数。1 以上、min(packets, n_features) 以下。

    Returns:
        主成分スコア・主軸・寄与率・時間平均。

    Raises:
        ValueError: 3 次元でない、NaN を含む、全パケットが同一、``n_components`` が範囲外の場合。
    """
    expected_ndim = 3  # (packets, subcarriers, streams)
    if amp.ndim != expected_ndim:
        raise ValueError(f"amp は 3 次元 (packets, subcarriers, streams) が必要: {amp.shape}")
    if np.isnan(amp).any():
        raise ValueError("amp に NaN があります（PCA の前に除去・補間してください）")
    n_packets = amp.shape[AXIS_PACKET]
    x = amp.reshape(n_packets, -1).astype(np.float64)
    n_features = x.shape[1]
    max_components = min(n_packets, n_features)
    if not 1 <= n_components <= max_components:
        raise ValueError(f"n_components は 1..{max_components} の範囲: {n_components}")

    mean = x.mean(axis=0)
    centered = x - mean
    # centered = U diag(s) Vt。Vt の行が主軸、U*s がスコア
    u, s, vt = np.linalg.svd(centered, full_matrices=False)
    total_power = float(np.sum(s**2))
    if total_power == 0.0:
        raise ValueError("全パケットが同一で変動がありません")

    components = vt[:n_components]
    signs = np.sign(components[np.arange(n_components), np.argmax(np.abs(components), axis=1)])
    components = components * signs[:, None]
    scores = u[:, :n_components] * s[:n_components] * signs
    ratio = s[:n_components] ** 2 / total_power
    return PcaResult(scores=scores, components=components, explained_variance_ratio=ratio,
                     mean=mean)


def sliding_temporal_std(amp: NDArray[np.float64], window: int) -> NDArray[np.float64]:
    """1 パケットずつずらした窓で、時間方向の標準偏差をサブキャリア・ストリームで平均する.

    ``windowed_temporal_std`` の窓を重ねる版（リアルタイム表示用）。位置 ``i`` の値は
    パケット ``i .. i+window-1`` の窓の値で、``i`` が ``window`` の倍数の位置では
    ``windowed_temporal_std`` と一致する。累積和で計算するので長い系列でも速い。

    Args:
        amp: 形状 (packets, subcarriers, streams) の振幅配列（NaN 不可）。
        window: 窓の長さ [packets]。

    Returns:
        形状 (packets - window + 1,) の指標（``amp`` と同じ単位）。

    Raises:
        ValueError: ``window`` が 2 未満、パケット数が ``window`` 未満、または NaN を含む場合。
    """
    min_window = 2  # 標準偏差には 2 点以上が要る
    if window < min_window:
        raise ValueError(f"window は {min_window} 以上: {window}")
    n_packets = amp.shape[AXIS_PACKET]
    if n_packets < window:
        raise ValueError(f"パケット数 {n_packets} が window={window} 未満です")
    if np.isnan(amp).any():
        raise ValueError("amp に NaN があります")
    x = amp.reshape(n_packets, -1).astype(np.float64)
    # 桁落ちを抑えるため、全体の平均を引いてから累積する
    x = x - x.mean(axis=0)
    zero = np.zeros((1, x.shape[1]))
    s1 = np.concatenate([zero, np.cumsum(x, axis=0)])
    s2 = np.concatenate([zero, np.cumsum(x**2, axis=0)])
    mean = (s1[window:] - s1[:-window]) / window
    var = (s2[window:] - s2[:-window]) / window - mean**2
    out: NDArray[np.float64] = np.sqrt(np.clip(var, 0.0, None)).mean(axis=1)
    return out


@dataclass(frozen=True)
class LockinResult:
    """``lockin`` の結果（実験05a: 周期的に動かした反射体の寄与を取り出す）.

    Attributes:
        freq_hz: 実際に採った周波数 [Hz]（``search_hz`` の範囲で最も強い周波数）。
        amplitude: 形状 (channels,) のその周波数の正弦波の振幅（入力と同じ単位、片側）。
        noise: 形状 (channels,) の近くの周波数の振幅の中央値（入力と同じ単位）。
        snr_db: 形状 (channels,) の SN 比 [dB] = 20·log10(amplitude / noise)。
    """

    freq_hz: float
    amplitude: NDArray[np.float64]
    noise: NDArray[np.float64]
    snr_db: NDArray[np.float64]


def dft_amplitude(time_sec: NDArray[np.float64], x: NDArray[np.float64],
                  freqs_hz: NDArray[np.float64]) -> NDArray[np.float64]:
    """不等間隔の時刻のまま、指定した周波数の正弦波の振幅を求める（ハン窓つき）.

    受信は約 100 Hz だが間隔がばらつき、欠番もある。等間隔を前提にする FFT の代わりに、
    実際の受信時刻で離散フーリエ変換を計算する。平均（静的成分）は先に引く。

    Args:
        time_sec: 形状 (n,) の時刻 [sec]（単調増加）。
        x: 形状 (n, channels) の時系列（例: サブキャリアごとの正規化振幅）。
        freqs_hz: 形状 (m,) の周波数 [Hz]。

    Returns:
        形状 (m, channels) の振幅（入力と同じ単位、片側。振幅 A の正弦波なら約 A）。

    Raises:
        ValueError: 形状が合わない、点が 2 つ未満、時刻が単調増加でない、NaN を含む場合。
    """
    min_points = 2
    if x.ndim != 2 or time_sec.shape != (x.shape[0],):
        raise ValueError(f"time_sec {time_sec.shape} と x {x.shape} の形が合いません")
    if len(time_sec) < min_points:
        raise ValueError(f"点が {min_points} つ以上必要です: {len(time_sec)}")
    if np.any(np.diff(time_sec) <= 0):
        raise ValueError("time_sec は単調増加である必要があります")
    if np.isnan(x).any() or np.isnan(time_sec).any():
        raise ValueError("NaN があります")
    t = time_sec - time_sec[0]
    span = t[-1]
    window = 0.5 - 0.5 * np.cos(2 * np.pi * t / span)  # ハン窓（両端 0）
    centered = x - x.mean(axis=0)
    weighted = centered * window[:, np.newaxis]
    phasor = np.exp(-2j * np.pi * np.outer(freqs_hz, t))  # (m, n)
    # 正弦波 A·cos(2πft) なら Σ w·x·e^{-j2πft} ≈ A/2 · Σ w。片側振幅に直す
    amp: NDArray[np.float64] = 2.0 * np.abs(phasor @ weighted) / window.sum()
    return amp


def lockin(time_sec: NDArray[np.float64], x: NDArray[np.float64], f_hz: float, *,
           search_hz: float = 0.02, guard_hz: float = 0.1,
           band_hz: float = 0.4) -> LockinResult:
    """周波数 ``f_hz`` の成分の強さと SN 比を、チャンネル（サブキャリア）ごとに求める.

    ``f_hz ± search_hz`` の範囲で全チャンネルの振幅の合計が最も大きい周波数を採る
    （手動や駆動のずれを許す）。雑音は ``f ± band_hz`` のうち ``f ± guard_hz`` を除いた
    周波数の振幅の中央値とする。周波数の刻みは 1 / (4·記録時間)。

    Args:
        time_sec: 形状 (n,) の時刻 [sec]。
        x: 形状 (n, channels) の時系列。
        f_hz: 狙う周波数 [Hz]（例: 反射体を回す周期 1 Hz）。
        search_hz: 周波数のずれを許す幅 [Hz]。
        guard_hz: 雑音の見積もりから除く幅 [Hz]（信号の裾を雑音に数えない）。
        band_hz: 雑音を見積もる片側の幅 [Hz]。

    Returns:
        採った周波数・振幅・雑音・SN 比。

    Raises:
        ValueError: 幅の大小関係が不正（0 < search_hz < guard_hz < band_hz でない）、
            ``f_hz - band_hz`` が 0 以下、または ``dft_amplitude`` の入力が不正な場合。
    """
    if not 0 < search_hz < guard_hz < band_hz:
        raise ValueError(f"0 < search_hz < guard_hz < band_hz が必要: "
                         f"{search_hz}, {guard_hz}, {band_hz}")
    if f_hz - band_hz <= 0:
        raise ValueError(f"f_hz - band_hz が 0 以下です: {f_hz} - {band_hz}")
    oversample = 4  # 周波数の刻み = 1 / (oversample · 記録時間)
    span_sec = float(time_sec[-1] - time_sec[0]) if len(time_sec) else 0.0
    step_hz = 1.0 / (oversample * span_sec) if span_sec > 0 else band_hz
    freqs = np.arange(f_hz - band_hz, f_hz + band_hz + step_hz / 2, step_hz, dtype=np.float64)
    amp = dft_amplitude(time_sec, x, freqs)
    offset = np.abs(freqs - f_hz)
    near = offset <= search_hz
    best = int(np.flatnonzero(near)[np.argmax(amp[near].sum(axis=1))])
    noise = np.median(amp[offset > guard_hz], axis=0)
    signal = amp[best]
    floor = np.finfo(np.float64).tiny  # 雑音 0 のときの log(0) 回避
    snr_db = 20.0 * np.log10(np.maximum(signal, floor) / np.maximum(noise, floor))
    return LockinResult(freq_hz=float(freqs[best]), amplitude=signal, noise=noise,
                        snr_db=snr_db.astype(np.float64))
