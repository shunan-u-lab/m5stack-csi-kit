"""ファームウェアの版をビルドに渡す（platformio.ini の extra_scripts から読む）.

    extra_scripts = pre:../shared/fw_version_pio.py

PlatformIO 自身の Python で動くので、``python3`` の無い Windows でもビルドできる
（元は ``build_flags = !python3 ../shared/fw_version.py`` で、Windows では落ちた）。
版の作り方は fw_version.py（-D CSI_FW_VERSION="1027-1a2b3c4" の形で渡す）。

失敗時の挙動: fw_version.py が git を呼べなければ "nogit" の版でビルドを続ける。
"""

import sys
from datetime import datetime
from pathlib import Path

Import("env")  # noqa: F821  PlatformIO（SCons）が与える

SHARED_DIR = Path(env.subst("$PROJECT_DIR")).resolve().parent / "shared"  # noqa: F821
sys.path.insert(0, str(SHARED_DIR))

import fw_version  # noqa: E402

env.Append(  # noqa: F821
    CPPDEFINES=[("CSI_FW_VERSION", env.StringifyMacro(fw_version.version(datetime.now())))]  # noqa: F821
)
