"""公開する文書が、作者の PC に結びつく場所・道具を前提にしていないこと（ADR 0008）.

他の人の環境の AI や学ぶ人が、無いファイル・無い道具を探して迷わないようにする。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

ROOT = Path(__file__).resolve().parent.parent

# 公開する文書（AI が読む入口・利用者向けの説明・チュートリアル）
PUBLIC_DOCS = sorted(
    [ROOT / name for name in ("AGENTS.md", "CLAUDE.md", "GEMINI.md")]
    + list(ROOT.glob("README*.md"))
    + list(ROOT.glob("DISCLAIMER*.md"))
    + list((ROOT / "docs" / "tutorial").rglob("*.md"))
)

# 作者の PC の場所（ホームの下）。例の `/home/you/` は使ってよい。
# 作者の道具・リポの名前はここに書かない（このファイルも公開されるため）
FORBIDDEN = {
    "ホームからのパス": re.compile(r"~/"),
    "/home/ の下のパス": re.compile(r"/home/(?!you/)"),
}


@pytest.mark.parametrize("path", PUBLIC_DOCS, ids=lambda p: str(p.relative_to(ROOT)))
def test_public_doc_has_no_author_specific_place(path: Path) -> None:
    """公開する文書に、作者の PC の場所が無い."""
    text = path.read_text(encoding="utf-8")
    found = [
        f"{what}（{i}行目）: {line.strip()[:60]}"
        for i, line in enumerate(text.splitlines(), 1)
        for what, pattern in FORBIDDEN.items()
        if pattern.search(line)
    ]
    assert not found, "\n".join(found)


def test_public_docs_are_found() -> None:
    """検査の対象が空になっていない（場所を移したときに検査が黙って空振りしない）."""
    names = {p.name for p in PUBLIC_DOCS}
    assert {"AGENTS.md", "README.md", "README.en.md"} <= names
    assert sum(p.parent.name == "tutorial" for p in PUBLIC_DOCS) >= 8


def test_agents_md_tells_ai_how_to_work_with_learners() -> None:
    """AGENTS.md に、学ぶ人と作業するときの振る舞い（確認を取る操作・出さない情報）がある."""
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    section = text.split("## 学ぶ人と作業するとき", 1)
    assert len(section) == 2, "「## 学ぶ人と作業するとき」の節が無い"
    body = section[1].split("\n## ", 1)[0]
    for word in ("flash", "PAIRCLEAR", "unpair", "確認", "MAC", "SSID", "docs/tutorial/"):
        assert word in body, f"節に {word} が無い"


CHAPTERS = sorted((ROOT / "docs" / "tutorial").glob("0[0-9]_*.md"))
NO_QUIZ = {"07_troubleshooting_glossary.md"}  # 困ったときの表と用語集なので、確かめようは置かない


def section(text: str, heading: str) -> str:
    """`## <heading>` の節の本文（次の `## ` まで）。無ければ空."""
    parts = text.split(f"\n## {heading}\n", 1)
    return parts[1].split("\n## ", 1)[0] if len(parts) == 2 else ""


@pytest.mark.parametrize("path", CHAPTERS, ids=lambda p: p.name)
def test_chapter_supports_three_ways_to_learn(path: Path) -> None:
    """各章に、一人で読むための答えと、AI 向けの例がある（ADR 0008 の 3 段目）."""
    text = path.read_text(encoding="utf-8")
    ai = section(text, "AI に聞いてみよう")
    assert ai, "「## AI に聞いてみよう」の節が無い"
    assert "コマンドライン AI" in ai, "コマンドライン AI 向けの頼み方の例が無い"
    assert "AI に貼らないもの" in ai, "AI に貼らないものが書かれていない"
    if path.name not in NO_QUIZ:
        quiz = section(text, "確かめよう")
        assert quiz, "「## 確かめよう」の節が無い"
        assert "答え</summary>" in quiz, "確かめようの答えが無い"


def test_tutorial_index_lists_three_ways_to_learn() -> None:
    """チュートリアルの入口に、一人で・チャット型の AI・コマンドライン AI の 3 つの読み方がある."""
    text = (ROOT / "docs" / "tutorial" / "README.md").read_text(encoding="utf-8")
    for way in ("一人で読む", "チャット型の AI と読む", "コマンドライン AI と動かす", "AGENTS.md"):
        assert way in text, f"{way} が無い"
    assert len(CHAPTERS) >= 9


def test_citation_version_matches_package() -> None:
    """CITATION.cff の版が pyproject.toml の版と同じ（版を上げたときの直し忘れを防ぐ）."""
    package = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    cff = (ROOT / "CITATION.cff").read_text(encoding="utf-8")
    found = re.search(r'^version:\s*"?([^"\s]+)"?\s*$', cff, flags=re.MULTILINE)
    assert found, "CITATION.cff に version が無い"
    assert found.group(1) == package["project"]["version"]
