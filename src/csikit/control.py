"""M5Stack Basic に命令を送る（チャンネル・送信出力の切り替え、組を消す）.

- ``set_channel``: 受信機に ``CH <n>`` を送る。受信機は署名つきの指示を無線で送信機に伝え、
  送信機の了解を待ってから自分も切り替える（2 台いっしょに変わる）。再起動すると ch11 に戻る
- ``set_tx_power``: 受信機に ``TXPS <dBm>`` を送り、送信機の出力を無線で変える。送信機は確認が
  来なければ 3 秒で元に戻る（firmware/csi-tx の README）。実際の出力は段階に丸められる
- ``unpair``: ``PAIRCLEAR`` を送り、組（相手の MAC と鍵）を消す。M5Stack Basic は再起動する

ポートは ``ports.open_no_reset`` で開く（再起動させない）。``PAIRSET`` はここからは送らない
（鍵を扱うのは ``pairing`` だけ。古いファームは知らない命令の行をそのまま返すため）。

失敗時の挙動: 範囲外の値は送る前に ControlError。M5Stack Basic が断った（``err``）ときは
``Reply.ok`` が False の結果を返す（理由は ``Reply.reason``）。時間内に応答が無ければ ControlError。
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from csikit import ports

CHANNEL_MIN = 1  # 日本で使えるチャンネルの範囲（firmware/csi-rx の受け付けと同じ）
CHANNEL_MAX = 13
HOME_CHANNEL = 11  # 起動時のチャンネル
TX_POWER_MIN_DBM = 2.0  # 送信出力として受け付ける範囲 [dBm]（ESP32 の上限設定は 2〜20 dBm）
TX_POWER_MAX_DBM = 20.0
# 応答を待つ時間 [sec]。CH は送信機の了解を最大 3 回待ち、TXPS はさらに 3 秒の確認がある
REPLY_TIMEOUT_SEC = 15.0
_PREFIX = {"tx": "[tx]", "rx": "#"}  # 応答の行の始まり（送信機・受信機）
_FIELD = re.compile(r"(\w+)=(\S+)")


class ControlError(RuntimeError):
    """命令を送れなかった・応答が無かった."""


@dataclass(frozen=True)
class Reply:
    """命令への応答.

    Attributes:
        ok: 了解（``ack``）なら True、断られた（``err``）なら False。
        fields: 応答の ``名前=値`` の組。
    """

    ok: bool
    fields: dict[str, str]

    @property
    def reason(self) -> str:
        """断られた理由（``reason=``。無ければ ``unknown``）."""
        return self.fields.get("reason", "unknown")


def send(board: ports.Board, command: str,
         opener: ports.Opener = ports.open_no_reset,
         clock: Callable[[], float] = time.monotonic,
         sleep: Callable[[float], None] = time.sleep,
         timeout_sec: float = REPLY_TIMEOUT_SEC) -> Reply:
    """1 台に命令を 1 行送り、その命令への ``ack`` か ``err`` を待つ.

    Args:
        board: 送り先（``WHO`` で見分けたもの。速さと応答の形を決める）。
        command: 命令の行（例: ``CH 6``）。``PAIRSET`` は送らない。
        opener: ポートを開く関数（テスト用）。
        clock: 時計（テスト用）。
        sleep: 待つ関数（テスト用）。
        timeout_sec: 応答を待つ時間 [sec]。

    Returns:
        応答。

    Raises:
        ControlError: ``PAIRSET`` を渡された・時間内に応答が無い。
    """
    name = command.split()[0] if command.split() else ""
    if not name or name == "PAIRSET":
        raise ControlError("この命令はここからは送りません"
                           "（組ませるのは csikit flash --pair-only）")
    prefix = _PREFIX[board.role]
    ack, err = f"{prefix} ack cmd={name}", f"{prefix} err cmd={name}"
    s = opener(board.port, board.baud)
    try:
        ports.flush_partial_line(s, sleep)
        s.write((command + "\n").encode("ascii"))
        deadline = clock() + timeout_sec
        while clock() < deadline:
            line = ports.read_line(s)
            if line.startswith(ack) or line.startswith(err):
                return Reply(ok=line.startswith(ack), fields=dict(_FIELD.findall(line)))
    finally:
        s.close()
    raise ControlError(f"{board.port}（{board.role}）から {timeout_sec:.0f} 秒以内に"
                       f" {name} の応答が無い（ファームが古い・別のアプリがポートを使っている、"
                       "など。csikit doctor で確認）")


def set_channel(rx: ports.Board, channel: int,
                opener: ports.Opener = ports.open_no_reset,
                clock: Callable[[], float] = time.monotonic,
                sleep: Callable[[float], None] = time.sleep) -> Reply:
    """受信機に ``CH <n>`` を送り、2 台のチャンネルを変える.

    Raises:
        ControlError: 受信機でない・範囲外・応答が無い。
    """
    _need_role(rx, "rx")
    if not CHANNEL_MIN <= channel <= CHANNEL_MAX:
        raise ControlError(f"チャンネルは {CHANNEL_MIN}〜{CHANNEL_MAX}: {channel}")
    return send(rx, f"CH {channel}", opener, clock, sleep)


def set_tx_power(rx: ports.Board, dbm: float,
                 opener: ports.Opener = ports.open_no_reset,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> Reply:
    """受信機に ``TXPS <dBm>`` を送り、送信機の出力を変える.

    Raises:
        ControlError: 受信機でない・範囲外・応答が無い。
    """
    _need_role(rx, "rx")
    if not TX_POWER_MIN_DBM <= dbm <= TX_POWER_MAX_DBM:
        raise ControlError(f"送信出力は {TX_POWER_MIN_DBM:g}〜{TX_POWER_MAX_DBM:g} dBm: {dbm:g}")
    return send(rx, f"TXPS {dbm:g}", opener, clock, sleep)


def unpair(board: ports.Board,
           opener: ports.Opener = ports.open_no_reset,
           clock: Callable[[], float] = time.monotonic,
           sleep: Callable[[float], None] = time.sleep) -> Reply:
    """``PAIRCLEAR`` を送り、組を消す（了解の後、M5Stack Basic は再起動する）.

    Raises:
        ControlError: 応答が無い。
    """
    return send(board, "PAIRCLEAR", opener, clock, sleep)


def _need_role(board: ports.Board, role: str) -> None:
    """役割を確かめる."""
    if board.role != role:
        name = "受信機" if role == "rx" else "送信機"
        raise ControlError(f"{board.port} は {board.role}。この命令は{name}に送ります")
