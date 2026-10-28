"""記録ファイル（data/raw）とメタデータ（data/meta）が揃っているかを点検する.

    uv run python scripts/check_meta.py                 # 点検だけ
    uv run python scripts/check_meta.py --draft         # メタデータのない記録に下書きを書く
    uv run python scripts/check_meta.py --allow-todo    # 未記入（TODO）は問題にしない

見るもの（書式は docs/decisions/0003-recording-metadata-format.md）:
- 欠け: 記録ファイルに対応するメタデータがない
- 形の問題: 読めない・必須項目がない・型や範囲が違う・ファイル名と recording が違う
- 未記入: TODO が残っている
- 記録のないメタデータ（別のマシンで取った等）は参考として表示するだけ

終了コード: 0 = 問題なし / 1 = 欠け・形の問題・未記入のどれかがある
（``--allow-todo`` なら未記入は除く）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from csikit import io, meta

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RAW_DIR = ROOT / "data" / "raw"
DEFAULT_META_DIR = ROOT / "data" / "meta"
MAX_TODOS_SHOWN = 5  # 1 記録あたりに表示する未記入の項目数


def main(argv: list[str] | None = None) -> int:
    """点検して結果を表示する.

    Args:
        argv: 引数（省略時は ``sys.argv``）。

    Returns:
        終了コード（0: 問題なし / 1: 問題あり・引数の誤り）。
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--meta-dir", type=Path, default=DEFAULT_META_DIR)
    parser.add_argument("--draft", action="store_true",
                        help="メタデータのない記録に下書きを書く（既存は上書きしない）")
    parser.add_argument("--experiment", default=meta.TODO, help="--draft で書く実験の番号")
    parser.add_argument("--allow-todo", action="store_true", help="未記入を問題にしない")
    args = parser.parse_args(argv)

    for d in (args.raw_dir, args.meta_dir):
        if not d.is_dir():
            print(f"ディレクトリがありません: {d}", file=sys.stderr)
            return 1

    report = meta.check_dirs(args.raw_dir, args.meta_dir)
    if args.draft and report.missing:
        for name in report.missing:
            try:
                out = meta.write_draft(io.find_raw(args.raw_dir, name), args.meta_dir,
                                       args.experiment)
            except (OSError, ValueError) as e:
                print(f"下書きを書けませんでした: {name}: {e}", file=sys.stderr)
            else:
                print(f"下書きを書きました: {out}")
        report = meta.check_dirs(args.raw_dir, args.meta_dir)

    print(f"記録 {report.checked} 本を点検しました（{args.raw_dir} ↔ {args.meta_dir}）")
    for name in report.missing:
        print(f"  欠け      {name}: メタデータがありません（--draft で下書きを書けます）")
    for name, problems in report.errors.items():
        for problem in problems:
            print(f"  形の問題  {name}: {problem}")
    for name, todos in report.todos.items():
        shown = ", ".join(todos[:MAX_TODOS_SHOWN])
        more = f" ほか {len(todos) - MAX_TODOS_SHOWN} 件" if len(todos) > MAX_TODOS_SHOWN else ""
        print(f"  未記入    {name}: {shown}{more}")
    for name in report.orphans:
        print(f"  参考      {name}: 記録ファイルがこのマシンにありません")

    failed = bool(report.missing or report.errors or (report.todos and not args.allow_todo))
    print("問題なし" if not failed else "問題があります")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
