"""csikit.display: モニタの窓を出せるかの判定."""

from __future__ import annotations

import pytest

from csikit import display


def no_env(name: str) -> str | None:
    """環境変数が何も無い."""
    return None


@pytest.mark.parametrize("backend", ["tkagg", "qtagg", "macosx"])
def test_interactive_backends_need_no_hint(backend: str) -> None:
    """窓を出せる描画先なら、他の条件に関係なく None."""
    assert display.no_window_hint(backend, "Linux", no_env, tkinter_ok=False) is None


def test_unknown_backend_is_not_interactive() -> None:
    """調べられなかったときは、窓を出せない側に倒す."""
    backend = f"{display.UNKNOWN_PREFIX}（ImportError: x）"
    assert not display.is_interactive(backend)
    assert display.no_window_hint(backend, "Linux", {"DISPLAY": ":0"}.get, True) is not None


def test_missing_tkinter_outside_linux_points_to_chapter_7() -> None:
    """Linux 以外で tkinter が無ければ、apt ではなく 7 章へ案内する."""
    hint = display.no_window_hint("agg", "Darwin", no_env, tkinter_ok=False)
    assert hint is not None
    assert "apt" not in hint and "7 章" in hint


def test_wayland_counts_as_a_screen() -> None:
    """WAYLAND_DISPLAY だけでも画面ありとみなす."""
    hint = display.no_window_hint("agg", "Linux", {"WAYLAND_DISPLAY": "wayland-0"}.get, True)
    assert hint is not None
    assert "画面の無い環境" not in hint


def test_plot_backend_returns_a_lowercase_name() -> None:
    """実際の matplotlib から描画先の名前が取れる（テストは Agg で動く）."""
    backend = display.plot_backend()
    assert backend == backend.lower()
    assert backend
