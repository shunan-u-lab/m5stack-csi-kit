"""記録（CSV）とメタデータ（TOML）の実機の MAC をダミーに置き換える（公開用のサンプルを作る）.

置き換え先（役割ごとに決める。``# who`` の行と CSI の行の送信元から役割を読む）:

- 受信機 → ``02:00:00:00:00:02``、送信機 → ``02:00:00:00:00:01``
- それ以外に見つかった MAC → ``02:00:00:00:00:1x``（見つかった順）

CSI の行は MAC が CRC の計算に入るので、**元の CRC が合うことを確かめてから**付け直す。
周りの Wi-Fi の情報（``bssid=``・``ssid``）を含む行がある記録は、伏せても公開しないので断る。
置き換えた後に、ダミー以外の MAC らしい文字列が残っていないかを確かめる（``find_leaks``）。

失敗時の挙動: CRC の合わない CSI の行・周りの Wi-Fi の情報・ダミーの数が足りないときは MaskError。
例外の文に実機の MAC は入れない（画面やログから漏れないように）。
"""

from __future__ import annotations

import re
import zlib
from collections.abc import Iterable

DUMMY_TX = "020000000001"  # 送信機のダミー（examples/ と同じ）
DUMMY_RX = "020000000002"  # 受信機のダミー
DUMMY_OTHER_BASE = 0x10  # その他の MAC のダミーの始まり（02:00:00:00:00:10〜）
DUMMY_OTHER_MAX = 0xEF  # その他の MAC のダミーの終わり
DUMMY_PREFIX = "0200000000"  # ダミーの先頭 10 桁（これで始まる MAC は伏せ済みとみなす）
ZERO_MAC = "0" * 12  # 「未設定」を表す MAC（伏せなくてよい）
_HEX12 = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{12}(?![0-9A-Fa-f])")
_COLON = re.compile(r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:])")
_WHO_RX = re.compile(r"\brole=rx\b.*?\bmac=([0-9A-Fa-f]{12})\b.*?\btx_mac=([0-9A-Fa-f]{12})\b")
_WHO_TX = re.compile(r"\brole=tx\b.*?\bmac=([0-9A-Fa-f]{12})\b(?:.*?\brx_mac=([0-9A-Fa-f]{12})\b)?")
# 注記の行で MAC とみなす 12 桁（mac=・tx_mac=・peer= などの後ろだけ。ただの 12 桁の数は除く）
_KEYED_HEX12 = re.compile(r"\b\w*(?:mac|peer)=([0-9A-Fa-f]{12})(?![0-9A-Fa-f])")
_WIFI = re.compile(r"\bb?ssid", re.IGNORECASE)
_CSI_PREFIX = "CSI,"
_CSI_TX_FIELD = 2  # format=3 の送信元の MAC の列（CSI,rx_us,tx,…）
_CSI_FIELDS_V3 = 17  # format=3 の列の数


class MaskError(RuntimeError):
    """伏せられなかった（理由に実機の MAC は含めない）."""


def compact(mac: str) -> str:
    """MAC を区切りなし・小文字の 12 桁にする."""
    return mac.replace(":", "").lower()


def colon(mac12: str) -> str:
    """12 桁の MAC を ``aa:bb:…`` の形にする."""
    return ":".join(mac12[i:i + 2] for i in range(0, 12, 2))


def is_masked(mac: str) -> bool:
    """伏せ済み（ダミー）か未設定の MAC か."""
    m = compact(mac)
    return m.startswith(DUMMY_PREFIX) or m == ZERO_MAC


def build_mapping(lines: Iterable[str]) -> dict[str, str]:
    """記録の行から、実機の MAC（12 桁）→ ダミー（12 桁）の対応を作る.

    Raises:
        MaskError: 周りの Wi-Fi の情報がある・ダミーが足りない。
    """
    mapping: dict[str, str] = {}
    others: list[str] = []

    def assign(mac: str, dummy: str | None = None) -> None:
        m = compact(mac)
        if is_masked(m) or m in mapping:
            return
        if dummy is not None:
            mapping[m] = dummy
        else:
            others.append(m)

    for line in lines:
        if line.startswith(_CSI_PREFIX):
            fields = line.split(",")
            if len(fields) == _CSI_FIELDS_V3:
                first_tx = DUMMY_TX not in mapping.values()  # 最初の送信元を送信機とみなす
                assign(fields[_CSI_TX_FIELD], DUMMY_TX if first_tx else None)
            continue
        if _WIFI.search(line):
            raise MaskError("周りの Wi-Fi の情報（ssid・bssid）を含む行があります。"
                            "この記録は公開用にしません")
        if (m := _WHO_RX.search(line)) is not None:
            assign(m.group(1), DUMMY_RX)
            assign(m.group(2), DUMMY_TX)
        elif (m := _WHO_TX.search(line)) is not None:
            assign(m.group(1), DUMMY_TX)
            if m.group(2):
                assign(m.group(2), DUMMY_RX)
        for found in _COLON.findall(line) + _KEYED_HEX12.findall(line):
            assign(found)
    for i, mac in enumerate(dict.fromkeys(o for o in others if o not in mapping)):
        n = DUMMY_OTHER_BASE + i
        if n > DUMMY_OTHER_MAX:
            raise MaskError("MAC が多すぎて、ダミーが足りません")
        mapping[mac] = f"{DUMMY_PREFIX}{n:02x}"
    return mapping


def mask_text(text: str, mapping: dict[str, str]) -> str:
    """CSI 以外の行（注記・TOML）の MAC を、12 桁・コロン区切りのどちらの形でも置き換える."""
    def hex12(m: re.Match[str]) -> str:
        return mapping.get(m.group(0).lower(), m.group(0))

    def with_colon(m: re.Match[str]) -> str:
        found = compact(m.group(0))
        return colon(mapping[found]) if found in mapping else m.group(0)

    return _HEX12.sub(hex12, _COLON.sub(with_colon, text))


def mask_csi_line(line: str, mapping: dict[str, str]) -> str:
    """CSI の行の送信元を置き換え、CRC を付け直す（元の CRC を先に確かめる）.

    Raises:
        MaskError: CRC が無い・合わない（壊れた行は伏せずに止める）。
    """
    body, sep, crc_hex = line.rpartition("*")
    if not sep or zlib.crc32(body.encode("ascii", errors="replace")) != int(crc_hex or "-1", 16):
        raise MaskError("CRC の合わない CSI の行があります（元の記録が壊れている）")
    fields = body.split(",")
    if len(fields) == _CSI_FIELDS_V3:
        fields[_CSI_TX_FIELD] = mapping.get(fields[_CSI_TX_FIELD].lower(), fields[_CSI_TX_FIELD])
    new_body = ",".join(fields)
    return f"{new_body}*{zlib.crc32(new_body.encode('ascii')):08x}"


def mask_lines(lines: Iterable[str], mapping: dict[str, str]) -> list[str]:
    """記録の行をすべて伏せる."""
    return [mask_csi_line(line, mapping) if line.startswith(_CSI_PREFIX) else
            mask_text(line, mapping) for line in lines]


def find_leaks(text: str) -> int:
    """ダミー・未設定以外の MAC らしい文字列の数（CSI の 16 進の本体は見ない）.

    見逃さない側に倒すため、キーの無いただの 12 桁も数える（数字だけの 12 桁があれば止まる）。
    """
    count = 0
    for line in text.splitlines():
        if line.startswith(_CSI_PREFIX):
            fields = line.split(",")
            line = fields[_CSI_TX_FIELD] if len(fields) == _CSI_FIELDS_V3 else ""
        count += sum(not is_masked(m) for m in _COLON.findall(line) + _HEX12.findall(line))
    return count
