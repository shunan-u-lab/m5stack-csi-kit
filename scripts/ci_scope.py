"""PR で変わったファイルから、CI で何を回すかを決める（.github/workflows/ci.yml の changes ジョブ）.

    git diff --name-only <base> HEAD | python3 scripts/ci_scope.py

標準入力に 1 行 1 ファイルのパスを受け、GitHub Actions の出力の形（``名前=値``）で 2 行を書く:

- ``python=full``  Python のテストを 3 OS で回す。``light`` なら Linux だけ
  （文書だけの変更でも回す。テストがチュートリアルの本文を検査しているため）
- ``firmware=true``  ファームのビルド（3 OS）を回す

決め方:

- 文書（``*.md``・``docs/`` の下・``LICENSE*``・``DISCLAIMER*``・``CITATION*``）だけなら
  ``python=light``・``firmware=false``
- ファーム・CI の設定・配布用の .bin を作るスクリプトが変われば、すべて回す
- それ以外（Python のコード・テスト・依存・例など、知らないファイルを含む）は
  ``python=full``・``firmware=false``

失敗時の挙動: 入力が空（変わったファイルが分からない）なら、安全側に倒してすべて回す。
標準ライブラリだけで書く（CI で uv sync の前に素の python3 で動かすため）。
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from typing import Final

DOC_SUFFIX: Final = ".md"
DOC_DIRS: Final = ("docs/",)
DOC_PREFIXES: Final = ("LICENSE", "DISCLAIMER", "CITATION")  # リポ直下のファイル名の先頭
# ファームのビルドが要る変更（ファームそのもの・CI の設定・.bin を作るスクリプト）
FIRMWARE_PATHS: Final = ("firmware/", ".github/", "scripts/make_dist.py")
FULL: Final = {"python": "full", "firmware": "true"}


def is_doc(path: str) -> bool:
    """文書だけのファイルか（テストは読むが、OS やファームには関係しない）."""
    return (path.endswith(DOC_SUFFIX) or path.startswith(DOC_DIRS)
            or ("/" not in path and path.startswith(DOC_PREFIXES)))


def needs_firmware(path: str) -> bool:
    """ファームのビルドが要る変更か（文書は除く）."""
    return not is_doc(path) and path.startswith(FIRMWARE_PATHS)


def scope(paths: Iterable[str]) -> dict[str, str]:
    """変わったファイルの一覧から、回すものを決める.

    Args:
        paths: リポの根からの相対パス（``/`` 区切り）。空行は無視する。

    Returns:
        ``{"python": "full" | "light", "firmware": "true" | "false"}``。
    """
    changed = [p.strip() for p in paths if p.strip()]
    if not changed:
        return dict(FULL)
    if any(needs_firmware(p) for p in changed):
        return dict(FULL)
    if all(is_doc(p) for p in changed):
        return {"python": "light", "firmware": "false"}
    return {"python": "full", "firmware": "false"}


def main() -> int:
    """標準入力のパスの一覧から、出力の 2 行を書く."""
    result = scope(sys.stdin)
    for key, value in result.items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
