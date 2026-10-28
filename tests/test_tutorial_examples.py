"""チュートリアルが動かすプログラム・本文の数値が、説明どおりであること."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
AUTH_HEADER = ROOT / "firmware" / "shared" / "CsiAuth" / "src" / "CsiAuth.h"


def load(name: str) -> ModuleType:
    """examples/tutorial/<name>.py を読み込む."""
    path = ROOT / "examples" / "tutorial" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


demo = load("multipath_demo")


def test_single_path_is_flat() -> None:
    """道が 1 本なら、どのサブキャリアも振幅は同じ（強め合い・打ち消し合いが無い）."""
    amp = np.abs(demo.channel(demo.subcarrier_freqs_hz(), [3.0], [1.0]))
    assert amp.shape == (56,) and np.allclose(amp, 1.0)


def test_half_wavelength_swaps_add_and_cancel() -> None:
    """2 本の道の長さの差が半波長変わると、強め合いと打ち消し合いが入れ替わる."""
    f = np.array([demo.CENTER_HZ])
    wavelength = demo.LIGHT_SPEED_M_S / demo.CENTER_HZ
    d = 3.0 + 10 * wavelength  # 長さの差が波長のちょうど整数倍 → 強め合う
    strong = abs(demo.channel(f, [3.0, d], [1.0, 0.5])[0])
    weak = abs(demo.channel(f, [3.0, d + wavelength / 2], [1.0, 0.5])[0])
    assert np.isclose(strong, 1.5) and np.isclose(weak, 0.5)


def test_moving_a_few_cm_changes_the_shape() -> None:
    """人で跳ね返る道が数 cm 変わると、56 本の形がはっきり変わる（チュートリアルの図の主張）."""
    amps = demo.amplitudes_for_moves()
    assert amps.shape == (len(demo.MOVES_CM), 56)
    change = np.abs(amps[-1] - amps[0]).mean() / amps[0].mean()
    assert change > 0.2  # 6 cm（ほぼ半波長）で平均 20% 以上変わる


def test_demo_saves_a_figure(tmp_path: Path) -> None:
    """--save で画面なしに図を保存できる."""
    out = tmp_path / "demo.png"
    assert demo.main(["--save", str(out)]) == 0 and out.stat().st_size > 0


def test_security_chapter_numbers_match_the_kit() -> None:
    """8 章の確かめ: 鍵は 16 進 32 文字、署名は 8 バイト、同じ中身と鍵なら同じ署名."""
    import hashlib
    import hmac

    from csikit import pairing

    assert len(pairing.new_key()) == 32
    key = bytes(16)
    tag = hmac.new(key, b"CH 6", hashlib.sha256).hexdigest()[:16]
    assert tag == hmac.new(key, b"CH 6", hashlib.sha256).hexdigest()[:16]
    assert tag != hmac.new(key, b"CH 7", hashlib.sha256).hexdigest()[:16]
    years = 2**64 / 1000 / 2 / 3600 / 24 / 365
    assert 2.8e8 < years < 3.0e8  # 本文の「約 2.9 億年」
    assert abs(10 ** (-8.5 / 30) - 0.52) < 0.01  # 本文の「約 0.52」
    tag_len_line = next(line for line in AUTH_HEADER.read_text(encoding="utf-8").splitlines()
                        if line.startswith("constexpr size_t TAG_LEN"))
    assert "= 8;" in tag_len_line  # 本文の「8 バイトの署名」


def test_connect_and_flash_chapter_commands() -> None:
    """2・3 章の確かめ: WHO の読み取り・書き込みの命令・PAIRSET の形（ダミーの値）."""
    from csikit import flash, pairing, ports

    who = ("# who role=rx mac=020000000002 channel=11 format=3 tx_mac=020000000001 relay=1 "
           "pico=0 auth=on tx_power_dbm=19.50 fw=1028-a74b527 paired=1")
    board = ports.parse_who(who)
    assert board is not None
    assert (board.role, board.mac, board.peer_mac, board.paired) == (
        "rx", "020000000002", "020000000001", True)
    image = Path("out/firmware/csi-tx-m5stack-basic.bin")
    cmd = flash.write_command("esptool", "/dev/ttyACM0", image)
    # 本文は Linux の表示。Windows では場所の区切りが \ になる（str(Path) は OS に従う）
    assert " ".join(cmd) == ("esptool --chip esp32 --port /dev/ttyACM0 --baud 460800 "
                             f"write-flash 0x0 {image}")
    assert pairing.pairset_command("020000000002", "0" * 32) == (
        "PAIRSET 020000000002 " + "0" * 32)
    assert ports.IDENTIFY_BAUDS == (921600, 115200)  # 2 章「2 つの速さで順に開き」


def test_csi_line_demo_reads_and_rejects_broken_line(capsys: pytest.CaptureFixture[str]) -> None:
    """4・5 章の確かめ: 練習用の行は 56 本・振幅 5 で読め、1 文字変えると CRC で捨てる."""
    demo = load("csi_line_demo")
    assert demo.main([]) == 0
    out = capsys.readouterr().out
    assert "使えるサブキャリア 56 本・振幅 5.0" in out and "送信元 02:00:00:00:00:01" in out
    assert demo.main(["--break"]) == 0
    assert "CRC が一致しません" in capsys.readouterr().out


def test_record_chapter_commands() -> None:
    """5 章の確かめ: 記録の名前の取り出し・見本のメタデータに TODO が無い."""
    from csikit import io, meta

    assert io.recording_name("data/raw/20261028-093000-empty.csv") == "20261028-093000-empty"
    assert meta.find_todos(meta.load_meta(ROOT / "examples/meta/20260101-120000-static.toml")) == []
    assert abs(19 / (5988 + 19) * 100 - 0.32) < 0.01  # 4 章「約 0.32」


def test_compare_motion_demo_matches_chapter_6(capsys: pytest.CaptureFixture[str]) -> None:
    """6 章の確かめ: 練習用データで static 約 0.019・wave 約 0.18（9.6 倍）."""
    cm = load("compare_motion")
    static = cm.motion_index(*cm.demo_shape("static"))
    wave = cm.motion_index(*cm.demo_shape("wave"))
    assert round(float(static.mean()), 3) == 0.019 and round(float(wave.mean()), 3) == 0.181
    assert static.size == wave.size == 29
    assert cm.main(["--demo"]) == 0
    assert "wave の平均は static の 9.6 倍" in capsys.readouterr().out


def test_motion_index_rejects_too_short_and_ignores_overall_gain() -> None:
    """1 秒より短い記録は断る。全体の強さを何倍にしても、形の揺れの数は変わらない."""
    cm = load("compare_motion")
    shape, t = cm.demo_shape("wave", seconds=3)
    with pytest.raises(ValueError, match="短い"):
        cm.motion_index(shape[:50], t[:50])
    amplitude = shape * np.linspace(1.0, 3.0, shape.shape[0])[:, None]  # 全体の強さが上がり続ける
    assert np.allclose(cm.motion_index(cm.shape_of(amplitude), t),
                       cm.motion_index(cm.shape_of(shape), t))
