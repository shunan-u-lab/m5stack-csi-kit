"""記録ごとのメタデータ（``data/meta/<記録名>.toml``）の読み書き・検証・下書き.

書式と判断の理由は docs/decisions/0003-recording-metadata-format.md。

- ``load_meta`` / ``dumps_meta``: TOML の読み書き（書き出しはこの書式で使う型だけに対応）
- ``validate_meta``: 形の誤り（必須項目の欠け・型・範囲・ファイル名との食い違い）を列挙する
- ``find_todos``: 未記入（``"TODO"``）の項目を列挙する。``"unknown"`` は「分からない」と
  書いた値なので数えない
- ``draft_from_capture``: 記録ファイルから自動で分かる項目（時刻・MAC・ファームの版・チャンネル・
  品質・印）を埋めた下書きを作る。人が書く項目は ``"TODO"`` にする
  （``base`` を渡せば前回の値を写す）
- ``check_dirs``: ``data/raw`` の CSV にメタデータが揃っているかを点検する
"""

from __future__ import annotations

import copy
import math
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, TypeGuard

from csikit import io

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover  Python 3.10 は tomli（pyproject で条件付きの依存）
    import tomli as tomllib

SCHEMA_VERSION = 1
META_SUFFIX = ".toml"
TEMPLATE_NAME = "TEMPLATE.toml"
TODO = "TODO"  # 未記入（点検で数える）
UNKNOWN = "unknown"  # 分からないと書いた値（未記入とは区別する）
PLACEHOLDERS = (TODO, UNKNOWN)
ROLES = ("tx", "rx")
FIRMWARE_BY_ROLE = {"tx": "csi-tx", "rx": "csi-rx"}
MAC_PATTERN = re.compile(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}")
CHANNEL_RANGE = (1, 14)  # 2.4 GHz 帯のチャンネル番号
ACTIVITY_MARKS = ("static", "walk", "empty")  # 区間を区切る印（live_csi の MARK_KEYS と対応）
BASELINE_MARK = "baseline"
SEGMENT_TOLERANCE_SEC = 1.0  # 区間の終わりが記録時間を超えてよい幅 [sec]
TIME_DECIMALS = 2  # 時刻 [sec] を丸める桁
RSSI_DECIMALS = 1  # RSSI [dBm] を丸める桁
BASE_SECTIONS = ("devices", "radio", "placement", "environment", "subjects")  # base から写す表
BARE_KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]+")
CAPTURE_START_PATTERN = re.compile(r"^# capture start=(\S+)")
RX_MAC_PATTERN = re.compile(r"(?:^#|\s)mac=([0-9a-f]{12})\b")
TX_LIST_PREFIX = "# tx "  # format=3 の送信機の一覧（# tx id=N mac=…）。受信機の MAC ではない
TX_MAC_PATTERN = re.compile(r"\btx_mac=([0-9a-f]{12})\b")
CHANNEL_PATTERN = re.compile(r"\bchannel=(\d+)\b")
KEY_VALUE_PATTERN = re.compile(r"(\w+)=(\S+)")
FW_TEXT_PATTERN = re.compile(r"[0-9A-Za-z._-]+")  # ファームの版の文字（例: 1028-3b8f078）
RX_FW_PATTERN = re.compile(r"\bfw=([0-9A-Za-z._-]+)")  # 受信機の起動の行・WHO の応答にある版
# 記録を始めるときに見つけた送信機の版（csikit record が書く）。
# MAC は入れない（受信機の MAC と取り違えないように）
TX_FW_PREFIX = "# capture tx_fw="

Meta = dict[str, Any]


def load_meta(path: str | Path) -> Meta:
    """メタデータのファイルを読む（検証はしない。``validate_meta`` を使う）.

    Args:
        path: TOML ファイルのパス。

    Returns:
        読み込んだ内容。

    Raises:
        FileNotFoundError: ファイルがない場合。
        ValueError: TOML として読めない場合。
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    try:
        data: Meta = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"{path}: TOML として読めません: {e}") from e
    return data


def _format_key(key: str) -> str:
    """TOML のキーを書く（記号を含むなら引用符で囲む）."""
    return key if BARE_KEY_PATTERN.fullmatch(key) else _format_value(key)


def _format_value(value: Any) -> str:
    """TOML の値（表以外）を書く.

    Raises:
        ValueError: この書式で使わない型、または NaN/inf の場合。
    """
    if isinstance(value, bool):  # int より先に判定する（bool は int の一種）
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"NaN/inf は書けません: {value}")
        return repr(value)
    if isinstance(value, str):
        escaped = []
        for ch in value:
            if ch == "\\":
                escaped.append("\\\\")
            elif ch == '"':
                escaped.append('\\"')
            elif ch == "\n":
                escaped.append("\\n")
            elif ch == "\t":
                escaped.append("\\t")
            elif ord(ch) < 0x20 or ord(ch) == 0x7F:
                escaped.append(f"\\u{ord(ch):04x}")
            else:
                escaped.append(ch)
        return '"' + "".join(escaped) + '"'
    if isinstance(value, list):
        return "[" + ", ".join(_format_value(v) for v in value) + "]"
    raise ValueError(f"書けない型です: {type(value).__name__}")


def _is_table_array(value: Any) -> TypeGuard[list[dict[str, Any]]]:
    """表の配列（``[[...]]``）か."""
    return isinstance(value, list) and len(value) > 0 and all(isinstance(v, dict) for v in value)


def _dump_table(prefix: str, table: dict[str, Any], out: list[str]) -> None:
    """表の中身を書く。値 → 子の表 → 子の表の配列の順に並べる."""
    for key, value in table.items():
        if not isinstance(value, dict) and not _is_table_array(value):
            out.append(f"{_format_key(key)} = {_format_value(value)}")
    for key, value in table.items():
        if isinstance(value, dict):
            name = prefix + _format_key(key)
            out.extend(["", f"[{name}]"])
            _dump_table(name + ".", value, out)
    for key, value in table.items():
        if _is_table_array(value):
            name = prefix + _format_key(key)
            for item in value:
                out.extend(["", f"[[{name}]]"])
                _dump_table(name + ".", item, out)


def dumps_meta(meta: Meta, header: list[str] | None = None) -> str:
    """メタデータを TOML の文字列にする.

    この書式で使う型（文字列・整数・小数・真偽・値の配列・表・表の配列）だけに対応する。
    表の中で、値は子の表より前に並べ替える（TOML の規則）。

    Args:
        meta: 書き出す内容。
        header: 先頭に付けるコメント行（``#`` は自動で付ける）。

    Returns:
        TOML の文字列（末尾に改行）。

    Raises:
        ValueError: 書けない型や NaN/inf を含む場合。
    """
    out = [f"# {line}" for line in (header or [])]
    _dump_table("", meta, out)
    return "\n".join(out).lstrip("\n") + "\n"


def _is_number(value: Any) -> TypeGuard[int | float]:
    """整数か小数か（真偽値は除く）."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check(problems: list[str], key: str, value: Any, kind: str, *,
           placeholder_ok: bool = True) -> None:
    """値の型を確かめ、違えば ``problems`` に足す.

    Args:
        problems: 問題の一覧（追記する）。
        key: 表示用のキーの位置。
        value: 値（``None`` は欠け）。
        kind: ``str`` / ``number`` / ``positive`` / ``int`` / ``bool`` / ``list``。
        placeholder_ok: ``"TODO"`` / ``"unknown"`` を許すか。
    """
    if value is None:
        problems.append(f"{key}: ありません")
        return
    if placeholder_ok and value in PLACEHOLDERS:
        return
    ok = {
        "str": isinstance(value, str) and value.strip() != "",
        "number": _is_number(value),
        "positive": _is_number(value) and value > 0,
        "int": isinstance(value, int) and not isinstance(value, bool),
        "bool": isinstance(value, bool),
        "list": isinstance(value, list),
    }[kind]
    if not ok:
        problems.append(f"{key}: 型または値が不正です（{kind} が必要）: {value!r}")


def _table(problems: list[str], meta: Meta, key: str) -> dict[str, Any]:
    """表を取り出す（なければ問題として記録し、空の表を返す）."""
    value = meta.get(key)
    if not isinstance(value, dict):
        problems.append(f"[{key}]: 表がありません")
        return {}
    return value


def _table_array(problems: list[str], meta: Meta, key: str, *,
                 required: bool) -> list[dict[str, Any]]:
    """表の配列を取り出す（必須でなければ、なくても問題にしない）."""
    value = meta.get(key)
    if value is None and not required:
        return []
    if not _is_table_array(value):
        problems.append(f"[[{key}]]: 1 つ以上の表が必要です")
        return []
    return list(value)


def validate_meta(meta: Meta, expected_recording: str | None = None) -> list[str]:
    """メタデータの形を検証し、問題を列挙する（未記入の ``"TODO"`` は数えない）.

    Args:
        meta: ``load_meta`` の結果。
        expected_recording: 期待する記録名（ファイル名から ``.toml`` を除いたもの）。

    Returns:
        問題の説明の一覧（空なら問題なし）。
    """
    problems: list[str] = []
    if meta.get("schema") != SCHEMA_VERSION:
        problems.append(f"schema: {SCHEMA_VERSION} が必要です: {meta.get('schema')!r}")
    for key in ("recording", "experiment", "label"):
        _check(problems, key, meta.get(key), "str", placeholder_ok=key != "recording")
    if expected_recording is not None and meta.get("recording") != expected_recording:
        problems.append(f"recording: ファイル名 {expected_recording!r} と一致しません: "
                        f"{meta.get('recording')!r}")

    time_table = _table(problems, meta, "time")
    start = time_table.get("start")
    _check(problems, "time.start", start, "str")
    if isinstance(start, str) and start not in PLACEHOLDERS:
        try:
            if datetime.fromisoformat(start).tzinfo is None:
                problems.append(f"time.start: タイムゾーンがありません: {start!r}")
        except ValueError:
            problems.append(f"time.start: ISO 8601 の時刻ではありません: {start!r}")
    duration = time_table.get("duration_sec")
    _check(problems, "time.duration_sec", duration, "positive")

    devices = _table_array(problems, meta, "devices", required=True)
    for i, dev in enumerate(devices):
        key = f"devices[{i}]"
        if dev.get("role") not in ROLES:
            problems.append(f"{key}.role: {ROLES} のどれかが必要です: {dev.get('role')!r}")
        for name in ("name", "firmware"):
            _check(problems, f"{key}.{name}", dev.get(name), "str")
        mac = dev.get("mac")
        _check(problems, f"{key}.mac", mac, "str")
        if isinstance(mac, str) and mac not in PLACEHOLDERS and not MAC_PATTERN.fullmatch(mac):
            problems.append(f"{key}.mac: 小文字・コロン区切りの MAC が必要です: {mac!r}")
    roles = {dev.get("role") for dev in devices}
    if devices and not set(ROLES) <= roles:
        found = sorted(str(r) for r in roles)
        problems.append(f"devices: 送信機（tx）と受信機（rx）が 1 台ずつ以上必要です: {found}")

    radio = _table(problems, meta, "radio")
    channel = radio.get("channel")
    _check(problems, "radio.channel", channel, "int")
    if isinstance(channel, int) and not CHANNEL_RANGE[0] <= channel <= CHANNEL_RANGE[1]:
        lo, hi = CHANNEL_RANGE
        problems.append(f"radio.channel: {lo}〜{hi} が必要です: {channel}")
    _check(problems, "radio.bandwidth", radio.get("bandwidth"), "str")
    _check(problems, "radio.mcs", radio.get("mcs"), "int")
    _check(problems, "radio.tx_rate_hz", radio.get("tx_rate_hz"), "positive")

    placement = _table(problems, meta, "placement")
    _check(problems, "placement.room", placement.get("room"), "str")
    _check(problems, "placement.tx_rx_distance_m", placement.get("tx_rx_distance_m"), "positive")
    if "line_of_sight" in placement:
        _check(problems, "placement.line_of_sight", placement["line_of_sight"], "bool")
    if "photos" in placement:
        _check(problems, "placement.photos", placement["photos"], "list", placeholder_ok=False)
    for i, node in enumerate(_table_array(problems, placement, "nodes", required=False)):
        _check(problems, f"placement.nodes[{i}].name", node.get("name"), "str")

    environment = _table(problems, meta, "environment")
    people = environment.get("people_in_room")
    _check(problems, "environment.people_in_room", people, "int")
    if isinstance(people, int) and not isinstance(people, bool) and people < 0:
        problems.append(f"environment.people_in_room: 0 以上が必要です: {people}")

    for i, subject in enumerate(_table_array(problems, meta, "subjects", required=False)):
        _check(problems, f"subjects[{i}].id", subject.get("id"), "str")

    for i, seg in enumerate(_table_array(problems, meta, "segments", required=True)):
        key = f"segments[{i}]"
        s, e = seg.get("start_sec"), seg.get("end_sec")
        _check(problems, f"{key}.start_sec", s, "number", placeholder_ok=False)
        _check(problems, f"{key}.end_sec", e, "number", placeholder_ok=False)
        if _is_number(s) and _is_number(e):
            if not 0 <= s < e:
                problems.append(f"{key}: 0 <= start_sec < end_sec が必要です: {s}〜{e}")
            if _is_number(duration) and e > duration + SEGMENT_TOLERANCE_SEC:
                problems.append(f"{key}.end_sec: 記録時間 {duration} 秒を超えています: {e}")
        for name in ("activity", "where"):
            _check(problems, f"{key}.{name}", seg.get(name), "str")

    for i, event in enumerate(_table_array(problems, meta, "events", required=False)):
        key = f"events[{i}]"
        _check(problems, f"{key}.t_sec", event.get("t_sec"), "number", placeholder_ok=False)
        _check(problems, f"{key}.note", event.get("note"), "str")
        _check(problems, f"{key}.confirmed", event.get("confirmed"), "bool")

    status = _table(problems, meta, "status")
    _check(problems, "status.usable", status.get("usable"), "bool")
    if "issues" in status:
        _check(problems, "status.issues", status["issues"], "list", placeholder_ok=False)
    return problems


def find_todos(value: Any, key: str = "") -> list[str]:
    """``"TODO"`` を含む項目の位置を列挙する.

    Args:
        value: メタデータ（またはその一部）。
        key: 表示用のキーの位置（再帰用）。

    Returns:
        キーの位置の一覧（例: ``placement.room``・``segments[0].where``）。
    """
    if isinstance(value, dict):
        return [k for name, v in value.items()
                for k in find_todos(v, f"{key}.{name}" if key else name)]
    if isinstance(value, list):
        return [k for i, v in enumerate(value) for k in find_todos(v, f"{key}[{i}]")]
    if isinstance(value, str) and TODO in value:
        return [key]
    return []


@dataclass(frozen=True)
class CaptureInfo:
    """記録ファイルから読み取った、自動で分かる項目.

    Attributes:
        start: 開始時刻（ISO 8601・タイムゾーン付き）。記録にない場合は ``"TODO"``。
        rx_mac / tx_mac: 受信機・送信機の MAC（小文字・コロン区切り。ない場合は None）。
        channel: チャンネル（ない場合は None）。
        mcs: 先頭パケットの MCS。
        duration_sec: 先頭から最後のパケットまでの時間 [sec]。
        rssi_mean_dbm: RSSI の平均 [dBm]。
        summary: ``# summary`` 行の key=value。
        marks: 印の (ラベル, 先頭パケットからの時刻 [sec]) の一覧（記録順）。
        rx_fw / tx_fw: 受信機・送信機のファームの版（記録にない場合は ``"unknown"``）。
    """

    start: str
    rx_mac: str | None
    tx_mac: str | None
    channel: int | None
    mcs: int
    duration_sec: float
    rssi_mean_dbm: float
    summary: dict[str, str]
    marks: list[tuple[str, float]] = field(default_factory=list)
    rx_fw: str = UNKNOWN
    tx_fw: str = UNKNOWN


def tx_fw_line(fw: str) -> str | None:
    """記録に残す送信機の版の行（``# capture tx_fw=…``）.

    Args:
        fw: 送信機の ``WHO`` の ``fw=`` の値。

    Returns:
        行（改行なし）。版の形でない（空・空白を含むなど）か ``"unknown"`` なら None。
    """
    if fw == UNKNOWN or not FW_TEXT_PATTERN.fullmatch(fw):
        return None
    return f"{TX_FW_PREFIX}{fw}"


def _colon_mac(hex12: str) -> str:
    """``02000000000b`` → ``02:00:00:00:00:0b``."""
    return ":".join(hex12[i:i + 2] for i in range(0, len(hex12), 2))


@dataclass
class _PacketStats:
    """CSI 行の集計（``read_capture_info`` が 1 行ずつ積み上げる。行そのものは持たない）.

    Attributes:
        first: 先頭の行。
        last_rx_us: 直前の行の受信時刻 [µs]（32bit で一周する）。
        elapsed_us: 先頭から直前の行までの経過時間 [µs]（``io.unwrap_rx_us`` と同じ補正）。
        n_packets: 行の数。
        rssi_sum_dbm: RSSI の合計 [dBm]。
        first_offset_us: 全部の行の先頭から、この集計の先頭の行までの経過時間 [µs]
            （送信機ごとの集計で、印の時刻を主の送信機の先頭からに直すのに使う）。
    """

    first: io.RxLine | None = None
    last_rx_us: int = 0
    elapsed_us: int = 0
    n_packets: int = 0
    rssi_sum_dbm: int = 0
    first_offset_us: int = 0

    def elapsed_at(self, rx_us: int) -> int:
        """受信時刻 rx_us が、先頭から何 µs 後かを返す（先頭の行がまだ無ければ 0）.

        rx_us は直前の行の近く（前後 32bit の一周の半分 = 約 35.8 分以内）とみなす。印の行は
        受信の順に書かれるが、送信機 2 台の行の前後や、印をまとめて後ろに書いた記録では、
        直前の行より少し前の時刻になることがある。

        Args:
            rx_us: 受信時刻 [µs]（32bit で一周する）。

        Returns:
            先頭からの経過時間 [µs]。
        """
        if self.first is None:
            return 0
        half = io.RX_TIMESTAMP_MODULUS // 2
        delta_us = (rx_us - self.last_rx_us + half) % io.RX_TIMESTAMP_MODULUS - half
        return self.elapsed_us + delta_us

    def add(self, rec: io.RxLine) -> None:
        """1 行を足す.

        Args:
            rec: 足す行。
        """
        if self.first is None:
            self.first = rec
        else:
            self.elapsed_us += (rec.rx_us - self.last_rx_us) % io.RX_TIMESTAMP_MODULUS
        self.last_rx_us = rec.rx_us
        self.n_packets += 1
        self.rssi_sum_dbm += rec.rssi_dbm


def read_capture_info(csv_path: str | Path) -> CaptureInfo:
    """記録ファイル（``capture_csi.py`` / ``live_csi.py --record``）から自動で分かる項目を読む.

    ファイルを 1 行ずつ読んで集計し、CSI を配列に読み込まない（数 GB の記録でもメモリは
    ほぼ一定。2.2 GB の記録を ``io.load_rx_csv`` で読んで 13 GB を超え、OOM で落ちたため）。
    送信機が 2 台以上の記録では、``io.load_rx_csv`` と同じ規則で主の送信機の行を集計する。
    印の時刻は、先頭パケットから印の ``rx_us`` までの経過時間。直前の行からの差を 32bit の一周で
    補正して足し上げるので、約 71.6 分（32bit の一周）より長い記録でもずれない（2026-10-09 まで
    先頭との差だけで出していて、18 時間の記録の 23:46 の印が 16:36 になっていた）。

    Args:
        csv_path: 記録ファイル。

    Returns:
        読み取った項目。

    Raises:
        FileNotFoundError: ファイルがない場合。
        ValueError: CSI 行がない・不正な（CRC が合わない等）場合、送信機が複数あるのに
            主の送信機が分からない場合（``io.load_rx_csv`` と同じ）。
    """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(f"CSI ファイルが見つかりません: {csv_path}")
    start, rx_mac, tx_mac, channel = TODO, None, None, None
    rx_fw, tx_fw = UNKNOWN, UNKNOWN
    summary: dict[str, str] = {}
    raw_marks: list[tuple[str, int]] = []  # (ラベル, 全部の行の先頭からの経過 [µs]。-1 = 時刻なし)
    primary: str | None = None
    overall = _PacketStats()
    by_tx: dict[str | None, _PacketStats] = {}
    with io.open_rx_text(csv_path) as f:
        for lineno, line in enumerate(f, start=1):
            if not line.strip():
                continue
            if not line.startswith(io.RX_COMMENT_PREFIX):
                try:
                    rec = io.parse_rx_line(line)
                except ValueError as e:
                    raise ValueError(f"{csv_path}:{lineno}: {e}") from e
                overall.add(rec)
                if rec.tx not in by_tx:
                    by_tx[rec.tx] = _PacketStats(first_offset_us=overall.elapsed_us)
                by_tx[rec.tx].add(rec)
                continue
            if (m := io.PRIMARY_TX_PATTERN.match(line)) and primary is None:
                primary = io.colon_mac(m.group(1) or m.group(2))
            if m := CAPTURE_START_PATTERN.match(line):
                naive_or_aware = datetime.fromisoformat(m.group(1))
                start = naive_or_aware.astimezone().isoformat(timespec="seconds")
            elif line.startswith(TX_FW_PREFIX):
                value = line[len(TX_FW_PREFIX):].strip()
                tx_fw = value if FW_TEXT_PATTERN.fullmatch(value) else UNKNOWN
            elif line.startswith("# summary"):
                summary = dict(KEY_VALUE_PATTERN.findall(line))
            elif line.startswith("# mark"):
                kv = dict(KEY_VALUE_PATTERN.findall(line))
                mark_rx_us = int(kv.get("rx_us", "-1"))
                raw_marks.append((kv.get("label", TODO),
                                  overall.elapsed_at(mark_rx_us) if mark_rx_us >= 0 else -1))
            else:
                if not line.startswith(TX_LIST_PREFIX) and (m := RX_MAC_PATTERN.search(line)):
                    rx_mac = _colon_mac(m.group(1))
                    if fw := RX_FW_PATTERN.search(line):
                        rx_fw = fw.group(1)
                if m := TX_MAC_PATTERN.search(line):
                    tx_mac = _colon_mac(m.group(1))
                if (m := CHANNEL_PATTERN.search(line)) and "mac=" in line:
                    channel = int(m.group(1))
    present = sorted(t for t in by_tx if t is not None)
    stats = overall
    if len(present) > 1:  # 送信機が 2 台以上なら主の送信機の行（io.load_rx_csv と同じ）
        if primary is None or primary not in present:
            raise ValueError(f"{csv_path}: 送信機が複数あります（{present}）。tx で選んでください")
        stats = by_tx[primary]
    first = stats.first
    if first is None:
        raise ValueError(f"{csv_path}: CSI 行がありません")
    # 主の送信機の先頭パケットからの時刻にする（先頭より前の印は 0 秒）
    marks = [(label, 0.0 if at_us < 0 else
              round(max(at_us - stats.first_offset_us, 0) / 1e6, TIME_DECIMALS))
             for label, at_us in raw_marks]
    return CaptureInfo(
        start=start, rx_mac=rx_mac, tx_mac=tx_mac, channel=channel, mcs=first.mcs,
        duration_sec=round(stats.elapsed_us / 1e6, TIME_DECIMALS),
        rssi_mean_dbm=round(stats.rssi_sum_dbm / stats.n_packets, RSSI_DECIMALS),
        summary=summary, marks=marks, rx_fw=rx_fw, tx_fw=tx_fw,
    )


def _segments_from_marks(info: CaptureInfo, label: str) -> list[dict[str, Any]]:
    """区間を作る。印がなければ全体を 1 区間、あれば区間を区切る印ごとに分ける."""
    cuts = [(t, m) for m, t in info.marks if m in ACTIVITY_MARKS]
    default = label if label in ACTIVITY_MARKS else TODO
    if not cuts:
        return [{"start_sec": 0.0, "end_sec": info.duration_sec, "activity": default,
                 "where": TODO}]
    segments = []
    if cuts[0][0] > 0:
        segments.append({"start_sec": 0.0, "end_sec": cuts[0][0], "activity": TODO,
                         "where": TODO})
    for (t, activity), nxt in zip(cuts, [*cuts[1:], (info.duration_sec, "")], strict=True):
        if nxt[0] > t:
            segments.append({"start_sec": t, "end_sec": nxt[0], "activity": activity,
                             "where": TODO})
    return segments


def _parse_number(text: str | None) -> int | float | str:
    """集計の値を数にする（なければ ``"unknown"``）."""
    if text is None:
        return UNKNOWN
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return UNKNOWN


def draft_from_capture(csv_path: str | Path, experiment: str = TODO,
                       base: Meta | None = None) -> Meta:
    """記録ファイルから、メタデータの下書きを作る.

    自動で分かる項目（開始時刻・記録時間・MAC・ファームの版・チャンネル・MCS・品質・印）を埋め、
    人が書く項目は ``"TODO"`` にする。``base`` を渡すと ``BASE_SECTIONS`` の表を写す
    （MAC・ファームの版・チャンネルは記録から分かれば記録の値で上書きし、MAC が違う機器の名前は
    ``"TODO"`` に戻す）。

    Args:
        csv_path: 記録ファイル（名前は ``<記録名>.csv``）。
        experiment: 実験の番号（例: ``exp04``）。
        base: 前回のメタデータ（配置などを引き継ぐ）。

    Returns:
        下書き（``validate_meta`` で形の問題がなく、``find_todos`` で未記入が並ぶ）。
    """
    csv_path = Path(csv_path)
    info = read_capture_info(csv_path)
    recording = io.recording_name(csv_path)
    label = recording.split("-", 2)[-1] if recording.count("-") >= 2 else recording
    base = copy.deepcopy(base) if base else {}

    macs = {"tx": info.tx_mac, "rx": info.rx_mac}
    fws = {"tx": info.tx_fw, "rx": info.rx_fw}
    devices = base.get("devices") or [
        {"role": role, "name": TODO, "board": TODO, "mac": TODO,
         "firmware": FIRMWARE_BY_ROLE[role], "firmware_rev": UNKNOWN} for role in ROLES]
    for dev in devices:
        mac = macs.get(str(dev.get("role")))
        if mac is not None:
            if dev.get("mac") not in (mac, TODO):
                dev["name"] = TODO  # 前回と違う M5Stack Basic。名前を確かめ直す
            dev["mac"] = mac
        fw = fws.get(str(dev.get("role")), UNKNOWN)
        if fw != UNKNOWN:
            dev["firmware_rev"] = fw

    radio = base.get("radio") or {"channel": TODO, "bandwidth": "HT20", "mcs": TODO,
                                  "tx_rate_hz": TODO}
    radio["channel"] = info.channel if info.channel is not None else radio.get("channel", TODO)
    radio["mcs"] = info.mcs

    events = []
    for mark_label, t in info.marks:
        if mark_label == BASELINE_MARK:
            events.append({"t_sec": t, "note": "静止基準を登録（b キー）", "confirmed": True})
        elif mark_label not in ACTIVITY_MARKS:
            events.append({"t_sec": t, "note": f"印（{mark_label}）: TODO 何が起きたかを書く",
                           "confirmed": False})

    summary = info.summary
    meta: Meta = {
        "schema": SCHEMA_VERSION,
        "recording": recording,
        "experiment": experiment,
        "label": label,
        "purpose": TODO,
        "time": {"start": info.start, "duration_sec": info.duration_sec},
        "devices": devices,
        "radio": radio,
        "placement": base.get("placement") or {"room": TODO, "tx_rx_distance_m": TODO,
                                               "line_of_sight": TODO, "photos": []},
        "environment": base.get("environment") or {"people_in_room": TODO,
                                                   "other_activity": TODO},
        "subjects": base.get("subjects") or [{"id": TODO, "role": TODO}],
        "segments": _segments_from_marks(info, label),
        "events": events,
        "quality": {
            "rate_hz": _parse_number(summary.get("rate_hz")),
            "missing_seq": _parse_number(summary.get("missing_seq")),
            "est_missing": _parse_number(summary.get("est_missing")),
            "bad_crc": _parse_number(summary.get("bad_crc")),
            "rssi_mean_dbm": info.rssi_mean_dbm,
        },
        "status": {"usable": TODO, "issues": []},
    }
    if not events:
        del meta["events"]
    return meta


def write_draft(csv_path: str | Path, meta_dir: str | Path, experiment: str = TODO,
                base_path: str | Path | None = None) -> Path:
    """記録ファイルの下書きを ``<meta_dir>/<記録名>.toml`` に書く（既存のファイルは上書きしない）.

    Args:
        csv_path: 記録ファイル。
        meta_dir: メタデータの置き場。
        experiment: 実験の番号。
        base_path: 配置などを引き継ぐ前回のメタデータ。

    Returns:
        書いたファイルのパス。

    Raises:
        FileExistsError: 同名のメタデータがある場合。
        FileNotFoundError: 記録ファイルか ``base_path`` がない場合。
        ValueError: 記録ファイルや ``base_path`` が不正な場合。
    """
    csv_path = Path(csv_path)
    out = Path(meta_dir) / f"{io.recording_name(csv_path)}{META_SUFFIX}"
    if out.exists():
        raise FileExistsError(f"既にあります（上書きしません）: {out}")
    base = load_meta(base_path) if base_path is not None else None
    meta = draft_from_capture(csv_path, experiment, base)
    header = [f"記録ツールが自動で書いた下書き（{datetime.now().isoformat(timespec='seconds')}）。"
              "TODO を埋める",
              "書式: docs/decisions/0003-recording-metadata-format.md"]
    if base_path is not None:
        header.append(f"{', '.join(BASE_SECTIONS)} は {Path(base_path).name} から写した。"
                      "変えたなら直す")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(dumps_meta(meta, header), encoding="utf-8")
    return out


@dataclass
class CheckReport:
    """``check_dirs`` の結果.

    Attributes:
        checked: 点検した記録の数。
        missing: メタデータがない記録名。
        errors: 記録名 → 形の問題（読めない場合も含む）。
        todos: 記録名 → 未記入の項目。
        orphans: 記録ファイルがないメタデータの記録名（別のマシンで取った等。問題とはしない）。
    """

    checked: int = 0
    missing: list[str] = field(default_factory=list)
    errors: dict[str, list[str]] = field(default_factory=dict)
    todos: dict[str, list[str]] = field(default_factory=dict)
    orphans: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """欠け・形の問題・未記入がないか."""
        return not (self.missing or self.errors or self.todos)


def check_meta_file(path: str | Path) -> tuple[list[str], list[str]]:
    """1 つのメタデータを読み、形の問題と未記入の項目を返す.

    Args:
        path: ``<記録名>.toml``。

    Returns:
        (形の問題, 未記入の項目)。読めなければ形の問題に理由が入る。
    """
    path = Path(path)
    try:
        meta = load_meta(path)
    except (OSError, ValueError) as e:
        return [str(e)], []
    return validate_meta(meta, expected_recording=path.stem), find_todos(meta)


def check_dirs(raw_dir: str | Path, meta_dir: str | Path) -> CheckReport:
    """記録ファイルとメタデータが揃っているか、メタデータが正しく埋まっているかを点検する.

    Args:
        raw_dir: 記録ファイル（``*.csv``・``*.csv.zst``）の置き場。
        meta_dir: メタデータ（``*.toml``）の置き場。雛形 ``TEMPLATE.toml`` は除く。

    Returns:
        点検の結果。
    """
    report = CheckReport()
    raw = {io.recording_name(p) for suffix in io.RAW_SUFFIXES
           for p in Path(raw_dir).glob(f"*{suffix}")}
    metas = {p.stem: p for p in Path(meta_dir).glob(f"*{META_SUFFIX}") if p.name != TEMPLATE_NAME}
    report.checked = len(raw)
    report.missing = sorted(raw - set(metas))
    report.orphans = sorted(set(metas) - raw)
    for name in sorted(metas):
        errors, todos = check_meta_file(metas[name])
        if errors:
            report.errors[name] = errors
        if todos:
            report.todos[name] = todos
    return report
