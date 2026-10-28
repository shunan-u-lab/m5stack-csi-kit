"""送信機と受信機を組ませる（相手の MAC と制御の鍵を、シリアルの PAIRSET で両方の NVS に置く）.

手順（docs/decisions/0002）:

1. 鍵を乱数で作る（16 バイト。組ごとに別）
2. 送信機に「受信機の MAC と鍵」、受信機に「送信機の MAC と鍵」を ``PAIRSET`` で送る。
   M5Stack Basic は NVS に置いて再起動する
3. 再起動を待ち、両方の ``WHO`` で ``paired=1`` と相手の MAC を確かめる

- 鍵は画面・例外の文・ログに出さない。古いファームは知らない命令を ``err cmd=<受け取った行>`` と
  そのまま返すので、``PAIRSET`` を知らない M5Stack Basic（``WHO`` に ``paired=`` が無い）には
  送らず、断られたときも応答の行は出さずに ``reason=`` の値だけを出す
- 受信機は再起動後、Wi-Fi を設定してあれば時刻の取得を待つので、確認は最大
  ``REBOOT_TIMEOUT_SEC`` 待つ

失敗時の挙動: M5Stack Basic が断った・答えない・確かめた結果が合わないときは PairError（理由つき）。
片方だけ組めた状態で止まりうるので、その場合はもう一度 ``csikit flash --pair-only`` を実行する
（PAIRSET は何度送ってもよい）。
"""

from __future__ import annotations

import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

from csikit import ports

KEY_BYTES = 16  # 鍵の長さ [byte]（firmware/shared/CsiAuth の KEY_LEN）
ACK_TIMEOUT_SEC = 3.0  # PAIRSET の了解を待つ時間 [sec]
# 再起動して WHO に答えるまで待つ時間 [sec]（受信機の NTP の待ち 最大 25 秒を含む）
REBOOT_TIMEOUT_SEC = 40.0
POLL_INTERVAL_SEC = 2.0  # 再起動を待つ間に WHO を試す間隔 [sec]
_MAC = re.compile(r"^[0-9a-f]{12}$")
_KEY = re.compile(r"^[0-9a-f]{32}$")
_PREFIX = {"tx": "[tx]", "rx": "#"}  # 応答の行の始まり（送信機・受信機）
_REASON = re.compile(r"\breason=(\w+)")


class PairError(RuntimeError):
    """組ませられなかった."""


@dataclass(frozen=True)
class PairResult:
    """組ませた結果（鍵は含めない）."""

    tx: ports.Board
    rx: ports.Board


def new_key(token_hex: Callable[[int], str] = secrets.token_hex) -> str:
    """鍵（16 進 32 文字）を乱数で作る.

    Args:
        token_hex: 乱数の関数（テスト用）。
    """
    return token_hex(KEY_BYTES)


def pairset_command(peer_mac: str, key: str) -> str:
    """``PAIRSET`` の行を作る（改行なし）.

    Raises:
        ValueError: MAC・鍵の形が違う（鍵の値は文に含めない）。
    """
    if not _MAC.match(peer_mac) or peer_mac == "0" * len(peer_mac):
        raise ValueError(f"相手の MAC の形が違う: {peer_mac!r}（16 進 12 文字・全部 0 は不可）")
    if not _KEY.match(key):
        raise ValueError("鍵の形が違う（16 進 32 文字・小文字）")
    return f"PAIRSET {peer_mac} {key}"


def send_pairset(board: ports.Board, peer_mac: str, key: str,
                 opener: ports.Opener = ports.open_no_reset,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
    """1 台に ``PAIRSET`` を送り、了解を待つ（了解の後、M5Stack Basic は再起動する）.

    Raises:
        PairError: 古いファーム・断られた・時間内に了解が無い。
    """
    if board.paired is None:
        raise PairError(f"{board.port}（{board.role}）のファームは組の命令を知りません"
                        f"（fw={board.fw}）。uv run csikit flash で書き直してください")
    line_text = pairset_command(peer_mac, key)
    prefix = _PREFIX[board.role]
    ack, err = f"{prefix} ack cmd=PAIRSET", f"{prefix} err cmd=PAIRSET"
    s = opener(board.port, board.baud)
    try:
        ports.flush_partial_line(s, sleep)
        s.write((line_text + "\n").encode("ascii"))
        deadline = clock() + ACK_TIMEOUT_SEC
        while clock() < deadline:
            line = ports.read_line(s)
            if line.startswith(ack):
                return
            if line.startswith(err):
                m = _REASON.search(line)  # 行そのものは鍵を含みうるので出さない
                reason = m.group(1) if m else "unknown"
                raise PairError(f"{board.port}（{board.role}）が断った: reason={reason}")
    finally:
        s.close()
    raise PairError(f"{board.port}（{board.role}）から {ACK_TIMEOUT_SEC:.0f} 秒以内に了解が無い"
                    "（ファームが古い可能性。WHO の fw= を確認）")


def wait_paired(board: ports.Board, want_peer: str,
                identify: Callable[[str], ports.Board | None] = ports.identify,
                clock: Callable[[], float] = time.monotonic,
                sleep: Callable[[float], None] = time.sleep) -> ports.Board:
    """再起動を待ち、組が効いたことを ``WHO`` で確かめる.

    Raises:
        PairError: 時間内に答えない・役割や相手が違う。
    """
    deadline = clock() + REBOOT_TIMEOUT_SEC
    last: ports.Board | None = None
    while clock() < deadline:
        sleep(POLL_INTERVAL_SEC)
        try:
            last = identify(board.port)
        except OSError:
            continue  # 再起動の間は USB が付き直してポートが一瞬消えることがある
        if last is not None and last.paired:
            break
    if last is None:
        raise PairError(f"{board.port}: 再起動後 {REBOOT_TIMEOUT_SEC:.0f} 秒以内に WHO に答えない")
    if last.role != board.role or last.mac != board.mac:
        raise PairError(f"{board.port}: 別の M5Stack Basic が答えた（{last.role} {last.mac}）")
    if not last.paired:
        raise PairError(f"{board.port}: 再起動後も paired=0（{last.who}）")
    if last.peer_mac != want_peer:
        raise PairError(f"{board.port}: 相手の MAC が違う（{last.peer_mac}、期待 {want_peer}）")
    return last


def pair(tx: ports.Board, rx: ports.Board, key: str | None = None,
         opener: ports.Opener = ports.open_no_reset,
         identify: Callable[[str], ports.Board | None] = ports.identify,
         clock: Callable[[], float] = time.monotonic,
         sleep: Callable[[float], None] = time.sleep) -> PairResult:
    """送信機と受信機を組ませ、確かめる.

    Args:
        tx: 送信機（``WHO`` で見分けたもの）。
        rx: 受信機。
        key: 鍵（None なら新しく作る。テスト用に与えられる）。
        opener: ポートを開く関数（テスト用）。
        identify: M5Stack Basic を見分ける関数（テスト用）。
        clock: 時計（テスト用）。
        sleep: 待つ関数（テスト用）。

    Returns:
        確かめた後の両方の M5Stack Basic。

    Raises:
        PairError: 役割が違う・断られた・確かめた結果が合わない。
    """
    if tx.role != "tx" or rx.role != "rx":
        raise PairError(f"役割が違う: {tx.port} は {tx.role}、{rx.port} は {rx.role}")
    secret = key if key is not None else new_key()
    send_pairset(tx, rx.mac, secret, opener, clock, sleep)
    send_pairset(rx, tx.mac, secret, opener, clock, sleep)
    del secret
    tx_after = wait_paired(tx, rx.mac, identify, clock, sleep)
    rx_after = wait_paired(rx, tx.mac, identify, clock, sleep)
    return PairResult(tx=tx_after, rx=rx_after)


def wait_role(port: str, role: str,
              identify: Callable[[str], ports.Board | None] = ports.identify,
              clock: Callable[[], float] = time.monotonic,
              sleep: Callable[[float], None] = time.sleep) -> ports.Board:
    """書き込み後の再起動を待ち、``port`` の M5Stack Basic が ``role`` として答えるのを確かめる.

    Raises:
        PairError: 時間内に答えない・役割が違う（送信機と受信機を逆のポートに書いた）。
    """
    deadline = clock() + REBOOT_TIMEOUT_SEC
    while True:
        try:
            board = identify(port)
        except OSError:
            board = None  # 書き込みの直後は USB が付き直してポートが一瞬消えることがある
        if board is not None:
            if board.role != role:
                raise PairError(f"{port} は {board.role} として答えた（{role} のはず）。"
                                "--tx-port と --rx-port を確かめる")
            return board
        if clock() >= deadline:
            raise PairError(f"{port}: {REBOOT_TIMEOUT_SEC:.0f} 秒以内に WHO に答えない"
                            "（書き込みに失敗したか、別のアプリがポートを使っている）")
        sleep(POLL_INTERVAL_SEC)
