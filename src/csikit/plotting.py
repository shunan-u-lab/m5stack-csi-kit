"""matplotlib で日本語を描けるようにする部品.

matplotlib の既定のフォント（DejaVu Sans / DejaVu Sans Mono）には日本語の字形が無く、
画面の日本語が □ になる（``UserWarning: Glyph ... missing from font(s)``）。

matplotlib（3.6 以降）は、字形が無い文字を ``family`` に並べた**次のファミリー**で描く。
ただし総称名（``sans-serif`` / ``monospace``）は最初に見つかった 1 つのフォントにしか
展開されないので、``font.monospace`` などの候補の後ろに足しても効かない。そこで:

- :func:`use_japanese_font` — 既定の ``font.family`` の後ろに日本語フォントを足す
- :func:`family_with_japanese` — ``family="monospace"`` のように総称名を直接指定する所で使う一覧

どちらも日本語フォントを後ろに置くので、英数字の見た目は今のまま、日本語だけが日本語フォントで
描かれる。
"""

from __future__ import annotations

import warnings
from collections.abc import Sequence
from typing import Final

import matplotlib as mpl
from matplotlib import font_manager

# 日本語フォントの候補（先にあるものを優先）。IPA ゴシックは等幅なので monospace の後ろにも合う
JAPANESE_FONT_CANDIDATES: tuple[str, ...] = (
    "IPAGothic",
    "IPAexGothic",
    "Noto Sans CJK JP",
    "Noto Sans Mono CJK JP",
    "Yu Gothic",
    "MS Gothic",
    "Hiragino Sans",
    "Droid Sans Fallback",
)
FAMILY_KEY: Final = "font.family"  # 既定のファミリーの一覧（rcParams のキー）


def find_japanese_font(candidates: Sequence[str] = JAPANESE_FONT_CANDIDATES) -> str | None:
    """入っている日本語フォントの名前を 1 つ返す.

    Args:
        candidates: 探すフォント名（先にあるものを優先）。

    Returns:
        見つかったフォント名。どれも無ければ ``None``。
    """
    installed = {f.name for f in font_manager.fontManager.ttflist}
    for name in candidates:
        if name in installed:
            return name
    return None


def family_with_japanese(generic: str,
                         candidates: Sequence[str] = JAPANESE_FONT_CANDIDATES) -> list[str]:
    """総称名の後ろに日本語フォントを付けた ``family`` の一覧を返す.

    Args:
        generic: 先頭に置くファミリー（例: ``"monospace"``）。
        candidates: 探すフォント名（先にあるものを優先）。

    Returns:
        ``[generic, 日本語フォント]``。日本語フォントが無ければ ``[generic]``。
    """
    name = find_japanese_font(candidates)
    return [generic] if name is None else [generic, name]


def use_japanese_font(candidates: Sequence[str] = JAPANESE_FONT_CANDIDATES) -> str | None:
    """既定の ``font.family`` の後ろに日本語フォントを足す.

    何度呼んでも同じ結果になる（既に入っていれば足さない）。見つからなければ警告を
    1 回出し、設定は変えない（日本語は □ のままだが、表示そのものは続けられる）。

    Args:
        candidates: 探すフォント名（先にあるものを優先）。

    Returns:
        使ったフォント名。見つからなければ ``None``。
    """
    name = find_japanese_font(candidates)
    if name is None:
        warnings.warn("日本語フォントが見つかりません（画面の日本語は □ になります）。"
                      "例: sudo apt install fonts-ipafont-gothic", stacklevel=2)
        return None
    families = list(mpl.rcParams[FAMILY_KEY])
    if name not in families:
        mpl.rcParams[FAMILY_KEY] = [*families, name]
    return name
