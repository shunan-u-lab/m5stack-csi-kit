"""ファームウェアの版を作る（fw_version_pio.py がビルドに渡す。単体で動かすと -D フラグを出す）.

    python fw_version.py   → '-DCSI_FW_VERSION="1007-db82545"'

版は「ビルドした日付（月日）-git の短いハッシュ」。コミットしていない変更があれば末尾に "+" を付ける
（例: 1007-db82545+）。M5Stack の液晶の上の帯と、シリアルの起動時の表示・WHO の応答（fw=）に出る。
空白を含めない（-D の値をシェルで分けないように）。

失敗時の挙動: git が無い・リポジトリの外なら、ハッシュの代わりに "nogit" を出す
（ビルドは止めない）。
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path

HASH_LEN = 7  # 短いハッシュの文字数
DIRTY_MARK = "+"  # コミットしていない変更があるときの印
NO_GIT = "nogit"


def git(*args: str) -> str | None:
    """Git を呼んで標準出力を返す（失敗なら None）.

    Args:
        *args: git の引数。

    Returns:
        前後の空白を除いた出力。
    """
    try:
        out = subprocess.run(["git", *args], cwd=Path(__file__).resolve().parent,
                             capture_output=True, text=True, check=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip()


def version(now: datetime) -> str:
    """版の文字列を作る.

    Args:
        now: ビルドの時刻。

    Returns:
        例 "1007-db82545+"。
    """
    rev = git("rev-parse", f"--short={HASH_LEN}", "HEAD")
    if not rev:
        return f"{now:%m%d}-{NO_GIT}"
    # firmware/ の下だけを見る（文書だけの変更で "+" が付かないように）
    dirty = git("status", "--porcelain", "--", str(Path(__file__).resolve().parent.parent))
    return f"{now:%m%d}-{rev}{DIRTY_MARK if dirty else ''}"


if __name__ == "__main__":
    sys.stdout.write(f"'-DCSI_FW_VERSION=\"{version(datetime.now())}\"'\n")
