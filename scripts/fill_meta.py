r"""複数のメタデータ（data/meta/*.toml）に、同じ値をまとめて書き込む.

    uv run python scripts/fill_meta.py data/meta/*-exp05b-s*-ch*.toml \
        --set 'purpose="ch1〜13 の掃引（3 回）"' \
        --set 'segments[*].activity="static"' \
        --set 'segments[*].where="P1 は T1–R1 線から外れた位置に座って動かない"' \
        --set 'status.usable=true'

``--set キー=値`` の値は TOML の値として書く（文字列は二重引用符で囲む。``true``・``3.0`` など）。
キーはドットでたどり、配列は ``名前[*]``（全要素）か ``名前[番号]`` で指す。

既定では**値が "TODO" の項目だけ**を書き換える（記入済みの値を誤って上書きしない）。
上書きするときは ``--force``。書き換えた結果を ``meta.validate_meta`` で確かめ、
形の問題が出るファイルは書き込まない。先頭のコメント行は残し、一括記入の注記を足す。

終了コード: 0 = 問題なし / 1 = 引数の誤り・読めない・形の問題で書き込めなかったファイルがある
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from csikit import meta

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

PART_PATTERN = re.compile(r"^([A-Za-z0-9_-]+)(?:\[(\*|\d+)\])?$")
ALL = "*"


def parse_assignment(text: str) -> tuple[list[tuple[str, str | None]], Any]:
    """``キー=値`` を読む.

    Args:
        text: 例 ``segments[*].where="席"``。

    Returns:
        (キーの各段（名前, 添字 or None）, 値)。

    Raises:
        ValueError: ``=`` がない・キーの形が不正・値が TOML として読めない場合。
    """
    key, sep, value = text.partition("=")
    if not sep:
        raise ValueError(f"キー=値 の形ではありません: {text}")
    parts = []
    for part in key.strip().split("."):
        m = PART_PATTERN.match(part)
        if m is None:
            raise ValueError(f"キーの形が不正です: {key}")
        parts.append((m.group(1), m.group(2)))
    try:
        parsed = tomllib.loads(f"v = {value.strip()}")["v"]
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"値が TOML として読めません（文字列は \"...\" で囲む）: {value}") from e
    return parts, parsed


def assign(node: Any, parts: list[tuple[str, str | None]], value: Any, force: bool,
           path: str = "") -> list[str]:
    """``node`` の中の指した項目に値を書く.

    Args:
        node: 表（dict）。
        parts: キーの各段。
        value: 書く値。
        force: TODO でない値も上書きするか。
        path: 表示用の位置（再帰用）。

    Returns:
        書き換えた位置の一覧（``TODO`` でないので飛ばした位置は ``"(skip) 位置"``）。

    Raises:
        KeyError: たどる途中の表や配列がない場合。
    """
    (name, index), rest = parts[0], parts[1:]
    here = f"{path}.{name}" if path else name
    if not isinstance(node, dict):
        raise KeyError(f"{path}: 表ではありません")
    if index is None:
        if rest:
            if name not in node:
                raise KeyError(f"{here}: ありません")
            return assign(node[name], rest, value, force, here)
        current = node.get(name)
        if current == value:
            return []
        if current is not None and current != meta.TODO and not force:
            return [f"(skip) {here}"]
        node[name] = value
        return [here]
    items = node.get(name)
    if not isinstance(items, list):
        raise KeyError(f"{here}: 配列がありません")
    targets = range(len(items)) if index == ALL else [int(index)]
    changed = []
    for i in targets:
        if i >= len(items):
            raise KeyError(f"{here}[{i}]: 範囲外です（{len(items)} 個）")
        item_path = f"{here}[{i}]"
        if rest:
            changed += assign(items[i], rest, value, force, item_path)
        elif items[i] != value and (items[i] == meta.TODO or force):
            items[i] = value
            changed.append(item_path)
        elif items[i] != value:
            changed.append(f"(skip) {item_path}")
    return changed


def leading_comments(text: str) -> list[str]:
    """ファイル先頭のコメント行（``# `` を除いた本文）を返す."""
    out = []
    for line in text.splitlines():
        if not line.startswith("#"):
            break
        out.append(line.lstrip("#").strip())
    return out


def main(argv: list[str] | None = None) -> int:
    """書き込む.

    Args:
        argv: 引数（省略時は ``sys.argv``）。

    Returns:
        終了コード。
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("files", type=Path, nargs="+", help="メタデータのファイル")
    parser.add_argument("--set", dest="assignments", action="append", required=True,
                        metavar="キー=値", help="書く値（繰り返せる）")
    parser.add_argument("--force", action="store_true", help="TODO でない値も上書きする")
    parser.add_argument("--dry-run", action="store_true", help="書き込まずに変更点だけ表示する")
    args = parser.parse_args(argv)

    try:
        assignments = [parse_assignment(a) for a in args.assignments]
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1

    failed = False
    for path in args.files:
        if path.name == meta.TEMPLATE_NAME:
            print(f"雛形は書き換えません: {path}", file=sys.stderr)
            continue
        try:
            text = path.read_text(encoding="utf-8")
            data = meta.load_meta(path)
            changed: list[str] = []
            for parts, value in assignments:
                changed += assign(data, parts, value, args.force)
        except (OSError, ValueError, KeyError) as e:
            print(f"{path.name}: {e}", file=sys.stderr)
            failed = True
            continue
        problems = meta.validate_meta(data, expected_recording=path.stem)
        if problems:
            print(f"{path.name}: 書き換えると形の問題が出るので書きません: {problems}",
                  file=sys.stderr)
            failed = True
            continue
        done = [c for c in changed if not c.startswith("(skip)")]
        skipped = [c for c in changed if c.startswith("(skip)")]
        todos = meta.find_todos(data)
        print(f"{path.name}: 変更 {len(done)}  飛ばした {len(skipped)}  残りの TODO {len(todos)}"
              + (f"  {', '.join(skipped)}" if skipped else ""))
        if done and not args.dry_run:
            header = leading_comments(text) + [
                f"fill_meta.py で一括記入（{datetime.now().isoformat(timespec='seconds')}）"]
            path.write_text(meta.dumps_meta(data, header), encoding="utf-8")
    if args.dry_run:
        print("（--dry-run: 書き込んでいません）")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
