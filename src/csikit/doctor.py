"""キットを使う前の点検（csikit doctor）. 何が足りないか・どう直すかを自分で教える.

見るもの（上から順。前の段が駄目でも、分かる範囲で最後まで見る）:

1. 道具: esptool（書き込み）。PlatformIO はファームを自分でビルドする人だけ
   （無くても NG にしない）。モニタの窓を出せるか（matplotlib の描画先。:mod:`csikit.display`）
2. ファーム: 書き込み用の .bin（``out/firmware/``）
3. USB のポート: M5Stack Basic の USB 変換チップ（CH9102・CP2104）が見えるか。
   Linux では読み書きの権限
4. M5Stack Basic: 各ポートに ``WHO`` を送り（リセットしない）、送信機・受信機が 1 台ずつ答えるか
5. 組: 両方が ``paired=1`` で、互いの MAC を相手として持っているか

失敗時の挙動: 点検そのものは例外で止めない。項目ごとに OK / NG / 情報 を返し、
NG には直し方を付ける。
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from csikit import display, flash, ports

MIN_PYTHON = (3, 10)
# Debian・Ubuntu で USB のシリアルを使える集団（Fedora・Arch は uucp）
LINUX_SERIAL_GROUP = "dialout"
DRIVER_HINTS = {  # USB 変換チップ → ドライバの入手先（Windows・Mac）
    "CH9102": "WCH の CH343SER（Windows）/ CH34xSER_MAC（Mac）。"
              "M5Stack の Docs の USB Driver の項から",
    "CP2104": "Silicon Labs の CP210x USB to UART Bridge VCP Drivers",
}


class Level(Enum):
    """点検の結果."""

    OK = "OK"
    NG = "NG"
    INFO = "--"


@dataclass(frozen=True)
class Check:
    """点検の 1 項目."""

    level: Level
    name: str
    detail: str
    hint: str = ""  # NG のときの直し方


@dataclass(frozen=True)
class Env:
    """点検に使う外の世界（テストで差し替える）."""

    which: Callable[[str], str | None] = shutil.which
    system: Callable[[], str] = platform.system
    list_ports: Callable[[], list[ports.PortInfo]] = ports.list_ports
    identify: Callable[[str], ports.Board | None] = ports.identify
    can_rw: Callable[[str], bool] = lambda dev: os.access(dev, os.R_OK | os.W_OK)
    dist_dir: Path = flash.DIST_DIR
    python: tuple[int, int] = (sys.version_info.major, sys.version_info.minor)
    plot_backend: Callable[[], str] = display.plot_backend
    has_tkinter: Callable[[], bool] = display.has_tkinter
    getenv: Callable[[str], str | None] = os.environ.get


def mac_text(mac: str) -> str:
    """12 桁の MAC を ``aa:bb:…`` の形にする."""
    return ":".join(mac[i:i + 2] for i in range(0, len(mac), 2))


def check_tools(env: Env) -> list[Check]:
    """Python・esptool・PlatformIO."""
    out = []
    if env.python >= MIN_PYTHON:
        out.append(Check(Level.OK, "Python", f"{env.python[0]}.{env.python[1]}"))
    else:
        out.append(Check(Level.NG, "Python", f"{env.python[0]}.{env.python[1]}",
                         "uv sync で入れ直す（pyproject.toml の requires-python）"))
    esptool = env.which(flash.ESPTOOL)
    if esptool:
        out.append(Check(Level.OK, "esptool", esptool))
    else:
        out.append(Check(Level.NG, "esptool", "見つからない",
                         "uv tool install esptool（入れた後、新しい端末を開く）"))
    pio = env.which("pio")
    out.append(Check(Level.INFO, "PlatformIO", pio or "無し（ファームを自分でビルドする人だけ要る:"
                                                      " uv tool install platformio）"))
    out.append(check_display(env))
    return out


def check_display(env: Env) -> Check:
    """モニタの窓を出せるか（matplotlib が画面を出せる描画先を選ぶか）."""
    backend = env.plot_backend()
    hint = display.no_window_hint(backend, env.system(), env.getenv, env.has_tkinter())
    if hint is None:
        return Check(Level.OK, "モニタの窓", f"描画先 {backend}")
    return Check(Level.NG, "モニタの窓", f"出せない（描画先 {backend}）", hint)


def check_images(env: Env) -> list[Check]:
    """書き込み用の .bin."""
    out = []
    for role in ("tx", "rx"):
        path = flash.default_image(role, env.dist_dir)
        if path.is_file():
            out.append(Check(Level.OK, f".bin（{role}）", str(path)))
        else:
            out.append(Check(Level.NG, f".bin（{role}）", f"無い: {path}",
                             "リリースの .bin を out/firmware/ に置くか、"
                             "uv run python scripts/make_dist.py --build"))
    return out


def driver_hint(system: str, infos: Sequence[ports.PortInfo]) -> str:
    """ポートが 1 つも見えないときの直し方（OS ごと）."""
    common = "データ通信のできる USB ケーブルか（充電専用では見えない）、本体の電源が入っているか"
    if system == "Linux":
        return (f"{common}を確かめる。WSL・仮想マシンなら USB をそちらに渡す"
                "（ChromeOS の Linux は「Linux と USB デバイスを共有」）")
    drivers = "／".join(f"{chip}: {where}" for chip, where in DRIVER_HINTS.items())
    return f"{common}を確かめ、USB ドライバを入れる（{drivers}）"


def check_ports(env: Env) -> tuple[list[Check], list[ports.PortInfo]]:
    """USB のポートと権限."""
    system = env.system()
    infos = env.list_ports()
    m5 = [p for p in infos if p.chip is not None]
    if not infos:
        return [Check(Level.NG, "USB のポート", "1 つも見えない", driver_hint(system, infos))], []
    out = []
    for p in infos:
        label = p.chip or "M5Stack Basic ではなさそう"
        out.append(Check(Level.INFO, "USB のポート", f"{p.device}（{label}・{p.description}）"))
    if not m5:
        out.append(Check(Level.NG, "M5Stack Basic", "USB 変換チップ（CH9102・CP2104）が見えない",
                         driver_hint(system, infos)))
    if system == "Linux":
        for p in m5:
            if not env.can_rw(p.device):
                out.append(Check(Level.NG, "権限", f"{p.device} を読み書きできない",
                                 f"sudo usermod -aG {LINUX_SERIAL_GROUP} $USER の後、"
                                 "ログインし直す（Fedora・Arch では uucp）"))
    # M5Stack に見えるポートが無ければ、全部を試す（チップの ID が違う版の可能性）
    return out, m5 or infos


def check_boards(env: Env, infos: Sequence[ports.PortInfo]
                 ) -> tuple[list[Check], list[ports.Board]]:
    """各ポートに WHO を送る（ポートが 1 つも無ければ、既に NG なので何も足さない）."""
    if not infos:
        return [], []
    out = []
    boards = []
    for p in infos:
        try:
            board = env.identify(p.device)
        except OSError as e:
            out.append(Check(Level.NG, "M5Stack Basic", f"{p.device}: {e}",
                             "シリアルモニタ・Arduino IDE など、ポートを使う他のアプリを閉じる"))
            continue
        if board is None:
            out.append(Check(Level.NG, "M5Stack Basic", f"{p.device}: WHO に答えない",
                             "このキットのファームが入っていない。uv run csikit flash で書き込む"))
            continue
        boards.append(board)
        name = "送信機（T1）" if board.role == "tx" else "受信機（R1）"
        out.append(Check(Level.OK, name, f"{p.device}  mac={mac_text(board.mac)}  fw={board.fw}"))
    for role, name in (("tx", "送信機（T1）"), ("rx", "受信機（R1）")):
        n = sum(b.role == role for b in boards)
        if n == 0:
            out.append(Check(Level.NG, name, "見つからない",
                             "2 台とも USB でつなぐ（書き込み前なら uv run csikit flash）"))
        elif n > 1:
            out.append(Check(Level.NG, name, f"{n} 台ある",
                             "このキットは送信機 1 台・受信機 1 台の組。"
                             "余分な M5Stack Basic を外す"))
    return out, boards


def check_pair(boards: Sequence[ports.Board]) -> list[Check]:
    """組の状態."""
    tx, rx = ports.pick_role(boards, "tx"), ports.pick_role(boards, "rx")
    if tx is None or rx is None:
        return []
    redo = "uv run csikit flash --pair-only で組ませ直す"
    if tx.paired is None or rx.paired is None:
        return [Check(Level.NG, "組", "ファームが古い（WHO に paired= が無い）",
                      "uv run csikit flash で書き直す")]
    if not tx.paired or not rx.paired:
        return [Check(Level.NG, "組", f"未設定（送信機 paired={int(tx.paired)}・"
                                       f"受信機 paired={int(rx.paired)}）", redo)]
    if tx.peer_mac != rx.mac or rx.peer_mac != tx.mac:
        return [Check(Level.NG, "組", "相手の MAC が合わない（別の M5Stack Basic と組んでいる）",
                      redo)]
    out = [Check(Level.OK, "組", "送信機と受信機が互いを相手にしている")]
    if tx.fw != rx.fw:
        out.append(Check(Level.INFO, "版", f"送信機 {tx.fw}・受信機 {rx.fw} で違う"
                                           "（動くが、揃えたほうがよい）"))
    return out


def run(env: Env | None = None) -> list[Check]:
    """全項目を点検する."""
    env = env or Env()
    checks = check_tools(env) + check_images(env)
    port_checks, candidates = check_ports(env)
    checks += port_checks
    board_checks, boards = check_boards(env, candidates)
    return checks + board_checks + check_pair(boards)


def report(checks: Sequence[Check]) -> str:
    """点検の結果を表示用の文にする."""
    lines = []
    for c in checks:
        lines.append(f"[{c.level.value}] {c.name}: {c.detail}")
        if c.level is Level.NG and c.hint:
            lines.append(f"       → {c.hint}")
    ng = sum(c.level is Level.NG for c in checks)
    lines.append("")
    lines.append("問題はありません。次は uv run csikit monitor" if ng == 0
                 else f"{ng} 件の問題があります（→ の手順で直してから、もう一度 csikit doctor）")
    return "\n".join(lines)
