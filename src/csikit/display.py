"""モニタの窓を出せるかを調べる（csikit doctor・csikit monitor が使う）.

matplotlib は、窓を出す部品（tkinter・Qt など）が見つからないと、黙って画面の無い描画先
``agg`` を選ぶ。その状態の ``plt.show()`` は何もせずにすぐ返るので、``csikit monitor`` は
窓を出さないまま終わる。よくある原因は 3 つ:

1. 環境変数 ``MPLBACKEND`` が画面の無い描画先を指定している
2. Python に tkinter が無い（Debian・Ubuntu・Chromebook の Linux の Python は、別のパッケージ
   ``python3-tk`` に分けている）
3. 画面の無い環境（SSH・Docker など。Linux で ``DISPLAY``・``WAYLAND_DISPLAY`` が無い）

失敗時の挙動: 調べられないとき（matplotlib の読み込みに失敗など）も例外で止めず、
描画先の名前の代わりに理由の文を返す（画面を出せない側として扱う）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

# 画面を出さない描画先（matplotlib の名前・小文字）
NON_INTERACTIVE_BACKENDS: Final = frozenset(
    {"agg", "pdf", "ps", "svg", "pgf", "cairo", "template"})
MPLBACKEND_ENV: Final = "MPLBACKEND"
DISPLAY_ENVS: Final = ("DISPLAY", "WAYLAND_DISPLAY")  # Linux で画面があるかの目印
UNKNOWN_PREFIX: Final = "調べられない"


def plot_backend() -> str:
    """描画先の名前（matplotlib が実際に選ぶもの・小文字）."""
    try:
        import matplotlib.pyplot as plt  # 描画先の自動選択は pyplot が決める
        return str(plt.get_backend()).lower()
    except Exception as e:  # noqa: BLE001  点検は止めない
        return f"{UNKNOWN_PREFIX}（{type(e).__name__}: {e}）"


def has_tkinter() -> bool:
    """Python に tkinter（窓を出す部品）があるか."""
    try:
        import tkinter  # noqa: F401
    except ImportError:
        return False
    return True


def is_interactive(backend: str) -> bool:
    """窓を出せる描画先か."""
    return not backend.startswith(UNKNOWN_PREFIX) and backend not in NON_INTERACTIVE_BACKENDS


def no_window_hint(backend: str, system: str, getenv: Callable[[str], str | None],
                   tkinter_ok: bool) -> str | None:
    """窓を出せないときの直し方（出せるなら None）.

    Args:
        backend: :func:`plot_backend` の結果。
        system: ``platform.system()`` の結果（``Linux``・``Windows``・``Darwin``）。
        getenv: 環境変数を読む関数。
        tkinter_ok: :func:`has_tkinter` の結果。
    """
    if is_interactive(backend):
        return None
    forced = getenv(MPLBACKEND_ENV)
    if forced:
        return (f"環境変数 {MPLBACKEND_ENV}={forced} が画面を出さない描画先を選んでいる。"
                f"外してから、もう一度（unset {MPLBACKEND_ENV}）")
    if not tkinter_ok:
        if system == "Linux":
            return ("Python に窓を出す部品（tkinter）が無い。sudo apt install python3-tk"
                    "（Debian・Ubuntu・Chromebook の Linux）")
        return "Python に窓を出す部品（tkinter）が無い。7 章の「monitor の窓が開かない」を見る"
    if system == "Linux" and not any(getenv(name) for name in DISPLAY_ENVS):
        return ("画面の無い環境（SSH・Docker など）で動いている。monitor は画面のある PC で動かす"
                "（記録だけなら csikit record は動く）")
    return "matplotlib が窓を出せる描画先を見つけられない。7 章の「monitor の窓が開かない」を見る"
