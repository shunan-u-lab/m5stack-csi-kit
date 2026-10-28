"""csikit.plotting のテスト（日本語フォントを代わりのファミリーに足す）."""

from __future__ import annotations

import warnings
from collections.abc import Iterator

import matplotlib as mpl
import pytest

from csikit import plotting

mpl.use("Agg")  # 画面なしで描く
import matplotlib.pyplot as plt  # noqa: E402

MISSING_FONT = "No Such Font For Test"  # どの環境にも無いフォント名
JAPANESE_TEXT = "連番が逆行しています（受信順の乱れ）"  # live_csi の見出しに出る文の例


@pytest.fixture(autouse=True)
def restore_rcparams() -> Iterator[None]:
    """テストごとに rcParams を元に戻す."""
    with mpl.rc_context():
        yield


def require_japanese_font() -> str:
    """日本語フォントの名前を返す（無い環境ではテストを飛ばす）."""
    name = plotting.find_japanese_font()
    if name is None:
        pytest.skip("日本語フォントが入っていない環境")
    return name


def draw_without_missing_glyph(**text_kwargs: object) -> None:
    """日本語の見出しを描き、字形が無いという警告が出ないことを確かめる."""
    fig = plt.figure()
    try:
        fig.suptitle(JAPANESE_TEXT, **text_kwargs)  # type: ignore[arg-type]
        with warnings.catch_warnings():
            warnings.filterwarnings("error", message="Glyph .* missing from font")
            fig.canvas.draw()
    finally:
        plt.close(fig)


def test_find_returns_none_when_no_candidate_installed() -> None:
    """候補がどれも無ければ None."""
    assert plotting.find_japanese_font([MISSING_FONT]) is None


def test_family_with_japanese_without_font_keeps_generic() -> None:
    """日本語フォントが無ければ総称名だけを返す."""
    assert plotting.family_with_japanese("monospace", [MISSING_FONT]) == ["monospace"]


def test_use_warns_and_keeps_rcparams_when_missing() -> None:
    """見つからなければ警告を出し、設定は変えない."""
    before = list(mpl.rcParams[plotting.FAMILY_KEY])
    with pytest.warns(UserWarning, match="日本語フォント"):
        assert plotting.use_japanese_font([MISSING_FONT]) is None
    assert list(mpl.rcParams[plotting.FAMILY_KEY]) == before


def test_use_appends_font_after_existing_and_is_idempotent() -> None:
    """日本語フォントは既定のファミリーの後ろに 1 回だけ足す."""
    name = require_japanese_font()
    before = ["sans-serif"]  # 他のテスト（live_csi の main）が先に足していても影響しないように
    mpl.rcParams[plotting.FAMILY_KEY] = before
    assert plotting.use_japanese_font() == name
    plotting.use_japanese_font()  # 2 回目は何も足さない
    assert list(mpl.rcParams[plotting.FAMILY_KEY]) == [*before, name]


def test_default_text_draws_japanese_after_use() -> None:
    """use_japanese_font の後は既定の文字で日本語の字形が欠けない."""
    require_japanese_font()
    plotting.use_japanese_font()
    draw_without_missing_glyph()


def test_monospace_text_draws_japanese_with_family_list() -> None:
    """family_with_japanese("monospace") なら等幅の見出しでも日本語の字形が欠けない."""
    require_japanese_font()
    draw_without_missing_glyph(family=plotting.family_with_japanese("monospace"))
