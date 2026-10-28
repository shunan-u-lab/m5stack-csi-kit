"""記録とメタデータの実機の MAC をダミーに置き換え、公開用の写しを別のフォルダに作る.

    uv run python scripts/mask_macs.py data/raw/20261028-114819-static.csv --out-dir out/sample
    uv run python scripts/mask_macs.py data/raw/*.csv --out-dir out/sample --meta-dir data/meta

各記録（``<名前>.csv``）と、同じ名前のメタデータ（``<meta-dir>/<名前>.toml``。あれば）を伏せて、
``<out-dir>/<名前>.csv``・``<out-dir>/<名前>.toml`` に書く。元のファイルは書き換えない。
置き換え先は受信機 ``02:00:00:00:00:02``・送信機 ``02:00:00:00:00:01``・その他 ``02:00:00:00:00:1x``
（``csikit.anonymize``）。実機の MAC は画面に出さない。

失敗時の挙動（その記録は書かず、次へ進む。1 つでも失敗すれば終了コード 1）:
- 出力先が元のフォルダと同じ・同じ名前の出力が既にある
- CRC の合わない CSI の行がある・周りの Wi-Fi の情報（ssid・bssid）がある
- 伏せた後にダミー以外の MAC らしい文字列が残る・伏せた記録を読み直して行の数が変わる
  （書きかけの出力は消す）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from csikit import anonymize, io

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_META_DIR = ROOT / "data" / "meta"
META_SUFFIX = ".toml"


def mask_one(csv_path: Path, meta_dir: Path, out_dir: Path) -> str:
    """1 つの記録（とメタデータ）を伏せて写す.

    Returns:
        結果の 1 行（実機の MAC は含まない）。

    Raises:
        anonymize.MaskError: 伏せられない・出力先が正しくない。
    """
    if csv_path.resolve().parent == out_dir.resolve():
        raise anonymize.MaskError("出力先が元の記録と同じフォルダです。"
                                  "別のフォルダを指定してください")
    name = io.recording_name(csv_path)
    out_csv = out_dir / f"{name}.csv"
    meta_path = meta_dir / f"{name}{META_SUFFIX}"
    out_meta = out_dir / f"{name}{META_SUFFIX}"
    for p in (out_csv, out_meta):
        if p.exists():
            raise anonymize.MaskError(f"{p.name} が既にあります（上書きしません）")
    lines = csv_path.read_text(encoding="utf-8").splitlines()
    mapping = anonymize.build_mapping(lines)
    masked_csv = "\n".join(anonymize.mask_lines(lines, mapping)) + "\n"
    masked_meta = (anonymize.mask_text(meta_path.read_text(encoding="utf-8"), mapping)
                   if meta_path.is_file() else None)
    for text in (masked_csv, masked_meta or ""):
        if (n := anonymize.find_leaks(text)) > 0:
            raise anonymize.MaskError(f"伏せた後に、ダミー以外の MAC らしい文字列が"
                                      f" {n} 個残りました")
    out_dir.mkdir(parents=True, exist_ok=True)
    written = [out_csv]
    try:
        out_csv.write_text(masked_csv, encoding="utf-8")
        before, after = io.load_rx_csv(csv_path), io.load_rx_csv(out_csv)
        if len(before.seq) != len(after.seq):
            raise anonymize.MaskError("伏せた記録を読み直すと、行の数が変わりました")
        if masked_meta is not None:
            written.append(out_meta)
            out_meta.write_text(masked_meta, encoding="utf-8")
    except BaseException:
        for p in written:
            p.unlink(missing_ok=True)
        raise
    meta_note = "メタデータも" if masked_meta is not None else "メタデータは無し"
    return f"{name}: CSI {len(after.seq)} 行・MAC {len(mapping)} 個を置き換えた（{meta_note}）"


def main(argv: list[str] | None = None) -> int:
    """伏せて写す.

    Args:
        argv: 引数（省略時は ``sys.argv``）。

    Returns:
        終了コード（0: 全部写せた / 1: 1 つでも失敗した）。
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("csv", nargs="+", type=Path, help="記録（data/raw/<名前>.csv）")
    parser.add_argument("--out-dir", type=Path, required=True,
                        help="写しの置き場（元と別のフォルダ）")
    parser.add_argument("--meta-dir", type=Path, default=DEFAULT_META_DIR,
                        help=f"メタデータの置き場（既定: {DEFAULT_META_DIR}）")
    args = parser.parse_args(argv)
    failed = 0
    for path in args.csv:
        try:
            print(mask_one(path, args.meta_dir, args.out_dir))
        except (anonymize.MaskError, OSError) as e:
            failed += 1
            print(f"{path.name}: 写しませんでした: {e}", file=sys.stderr)
        except ValueError:  # 読み込みの例外の文は行の一部（MAC を含みうる）を含むので出さない
            failed += 1
            print(f"{path.name}: 写しませんでした: 記録として読めない行があります", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
