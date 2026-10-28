"""scripts/ci_scope.py: 変わったファイルから CI で回すものを決める."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "ci_scope.py"


def load() -> ModuleType:
    """スクリプトをモジュールとして読む."""
    spec = importlib.util.spec_from_file_location("ci_scope", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ci_scope = load()
LIGHT = {"python": "light", "firmware": "false"}
PYTHON = {"python": "full", "firmware": "false"}
FULL = {"python": "full", "firmware": "true"}


@pytest.mark.parametrize(("paths", "expected"), [
    (["docs/tasks/current.md", "README.md", "README.en.md"], LIGHT),
    (["docs/tutorial/images/fig.png", "LICENSE", "DISCLAIMER.en.md"], LIGHT),
    (["CITATION.cff", "README.md"], LIGHT),
    (["firmware/csi-tx/README.md"], LIGHT),
    (["docs/tasks/current.md", "scripts/live_csi.py"], PYTHON),
    (["src/csikit/doctor.py"], PYTHON),
    (["uv.lock"], PYTHON),
    (["examples/meta/example.toml"], PYTHON),
    (["firmware/csi-rx/src/main.cpp", "README.md"], FULL),
    ([".github/workflows/ci.yml"], FULL),
    (["scripts/make_dist.py"], FULL),
])
def test_scope(paths: list[str], expected: dict[str, str]) -> None:
    """文書だけなら軽く、Python なら 3 OS、ファーム・CI の設定ならすべて."""
    assert ci_scope.scope(paths) == expected


def test_unknown_changes_run_everything() -> None:
    """変わったファイルが分からない（空）なら、安全側に倒してすべて回す."""
    assert ci_scope.scope(["", "  \n"]) == FULL


def test_license_only_at_the_root() -> None:
    """LICENSE で始まる名前を文書とみなすのはリポ直下だけ."""
    assert ci_scope.scope(["src/csikit/LICENSE_check.py"]) == PYTHON
