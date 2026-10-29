"""キットの入口（uv run csikit <命令>）.

    uv run csikit doctor                     # 点検（足りないもの・直し方を表示）
    uv run csikit flash                      # 2 台に書き込み、組ませる
    uv run csikit flash --pair-only          # 書き込まずに組ませ直す
    uv run csikit monitor                    # 受信のモニタ（振幅・レート・欠け・RSSI）
    uv run csikit record --duration 60 --label empty   # 記録（scripts/capture_csi.py）
    uv run csikit channel 6                  # 2 台のチャンネルを変える（再起動すると 11 に戻る）
    uv run csikit txpower 8                  # 送信機の出力を変える [dBm]（再起動すると既定に戻る）
    uv run csikit unpair                     # 組（相手の MAC と鍵）を消す（人に渡す前に）

ポートは指定しなければ ``WHO`` で探す。どの命令も受信機をリセットしない（record は ``--reset`` を
付けたときだけリセットする）。record は残りの引数を scripts/capture_csi.py にそのまま渡す
（例: ``csikit record … --no-meta``）。多機能のビューアは scripts/live_csi.py。

失敗時の挙動: 想定した失敗（ポートが無い・M5Stack Basic が答えない・書き込みや組ませる手順の
失敗）は、理由と直し方を表示して終了コード 1 で終わる。
"""

from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Sequence
from pathlib import Path

from csikit import control, doctor, flash, meta, pairing, ports

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"


class CliError(RuntimeError):
    """利用者に見せる失敗（理由と直し方）."""


def find_role(role: str) -> ports.Board:
    """役割（tx/rx）の M5Stack Basic をポートから探す.

    Raises:
        CliError: 見つからない・2 台以上ある。
    """
    boards, silent = ports.find_boards(ports.list_ports())
    return require_role(boards, silent, role)


def require_role(boards: Sequence[ports.Board], silent: dict[str, str], role: str) -> ports.Board:
    """見つけた M5Stack Basic から、役割（tx/rx）の 1 台を選ぶ.

    Raises:
        CliError: 見つからない・2 台以上ある。
    """
    board = ports.pick_role(boards, role)
    if board is None:
        n = sum(b.role == role for b in boards)
        name = "送信機" if role == "tx" else "受信機"
        reason = "見つかりません" if n == 0 else f"{n} 台あります"
        raise CliError(f"{name}が{reason}（答えなかったポート: {', '.join(silent) or 'なし'}）。"
                       "uv run csikit doctor で確認してください")
    return board


def cmd_doctor(_: argparse.Namespace) -> int:
    """点検する."""
    checks = doctor.run()
    print(doctor.report(checks))
    return 1 if any(c.level is doctor.Level.NG for c in checks) else 0


def resolve_pair_ports(args: argparse.Namespace) -> tuple[str, str]:
    """書き込む先（送信機・受信機のポート）を決める.

    指定が無ければ WHO で探す。まだキットのファームが入っていない M5Stack Basic は答えないので、
    その場合は ``--tx-port`` と ``--rx-port`` を指定してもらう。
    """
    if args.tx_port and args.rx_port:
        if args.tx_port == args.rx_port:
            raise CliError("--tx-port と --rx-port が同じです")
        return args.tx_port, args.rx_port
    boards, _ = ports.find_boards(ports.list_ports())
    tx = args.tx_port or getattr(ports.pick_role(boards, "tx"), "port", None)
    rx = args.rx_port or getattr(ports.pick_role(boards, "rx"), "port", None)
    if tx is None or rx is None:
        found = ", ".join(f"{b.port}={b.role}" for b in boards) or "なし"
        raise CliError(
            f"送信機・受信機のポートを決められません（WHO に答えた M5Stack Basic: {found}）。\n"
            "  まだ書き込んでいない M5Stack Basic は WHO に答えません。1 台ずつつないで\n"
            "  uv run csikit doctor でポート名を確かめ、\n"
            "  --tx-port <T1 にするポート> --rx-port <R1 にするポート> を指定してください")
    if tx == rx:
        raise CliError(f"送信機と受信機が同じポート（{tx}）になりました。"
                       "--tx-port と --rx-port を指定してください")
    return tx, rx


def confirm(text: str, assume_yes: bool) -> bool:
    """確認を取る（--yes なら聞かない）."""
    if assume_yes:
        return True
    return input(f"{text} [y/N] ").strip().lower() in ("y", "yes")


def cmd_flash(args: argparse.Namespace) -> int:
    """書き込んで組ませる."""
    tx_port, rx_port = resolve_pair_ports(args)
    if not args.pair_only:
        esptool = flash.find_esptool()
        images = {"tx": flash.check_image(args.tx_bin or flash.default_image("tx")),
                  "rx": flash.check_image(args.rx_bin or flash.default_image("rx"))}
        print(f"送信機（T1）: {tx_port} ← {images['tx']}")
        print(f"受信機（R1）: {rx_port} ← {images['rx']}")
        print("書き込むと、M5Stack Basic の元の中身（買ったときのデモなど）は消えます。"
              "残すなら先に scripts/backup_flash.py で吸い出してください")
        if not confirm("書き込みますか", args.yes):
            print("やめました")
            return 1
        flash.write_image(tx_port, images["tx"], esptool)
        flash.write_image(rx_port, images["rx"], esptool)
    tx = pairing.wait_role(tx_port, "tx")
    rx = pairing.wait_role(rx_port, "rx")
    print("組ませています（両方が再起動します。受信機は最大 40 秒ほどかかります）")
    result = pairing.pair(tx, rx)
    for name, board in (("送信機（T1）", result.tx), ("受信機（R1）", result.rx)):
        print(f"{name}: {board.port}  mac={doctor.mac_text(board.mac)}  fw={board.fw}")
    print("組ませました。次は uv run csikit monitor")
    return 0


# 断られたときの理由 → 直し方（受信機の CH・TXPS の reason=）
REASON_HINTS = {
    "tx_no_ack": "送信機から了解が来ません。2 台とも元のチャンネルのままです。"
                 "送信機の電源・距離と、組（uv run csikit doctor）を確かめてください",
    "tx_nack": "送信機が断りました（範囲外など）。2 台とも元のままです",
    "lost_link": "新しい出力で送信機の電波が届かなかったので、送信機は元の出力に戻りました",
    "out_of_range": "値が受け付ける範囲の外です",
    "no_key": "組が未設定です。uv run csikit flash --pair-only で組ませてください",
}


def refused(reply: control.Reply) -> str:
    """断られたときの文."""
    hint = REASON_HINTS.get(reply.reason, "")
    return f"断られました（reason={reply.reason}）。{hint}"


def receiver(port: str | None) -> ports.Board:
    """受信機を見分ける（ポートの指定が無ければ探す）.

    Raises:
        CliError: 指定のポートが受信機として答えない・見つからない。
    """
    if port is None:
        return find_role("rx")
    board = ports.identify(port)
    if board is None or board.role != "rx":
        raise CliError(f"{port} は受信機として答えません。uv run csikit doctor で確認してください")
    return board


def cmd_channel(args: argparse.Namespace) -> int:
    """2 台のチャンネルを変える."""
    rx = receiver(args.port)
    reply = control.set_channel(rx, args.channel)
    if not reply.ok:
        print(refused(reply), file=sys.stderr)
        return 1
    print(f"チャンネルを {reply.fields.get('channel')} にしました（送信機も了解。"
          f"送り直し {int(reply.fields.get('attempts', '1')) - 1} 回）")
    print(f"M5Stack Basic を再起動すると ch{control.HOME_CHANNEL} に戻ります")
    return 0


def cmd_txpower(args: argparse.Namespace) -> int:
    """送信機の出力を変える."""
    rx = receiver(args.port)
    reply = control.set_tx_power(rx, args.dbm)
    if not reply.ok:
        print(refused(reply), file=sys.stderr)
        return 1
    print(f"送信機の出力を {reply.fields.get('dbm')} dBm にしました（指定 {args.dbm:g} dBm。"
          "出力は段階に丸められます）")
    print("記録の途中では変えないでください。既定の出力に戻すには、送信機を再起動します")
    return 0


def cmd_unpair(args: argparse.Namespace) -> int:
    """つながっている M5Stack Basic の組を消す."""
    boards, _ = ports.find_boards(ports.list_ports())
    if not boards:
        raise CliError("WHO に答える M5Stack Basic が見つかりません。"
                       "uv run csikit doctor で確認してください")
    for b in boards:
        name = "送信機（T1）" if b.role == "tx" else "受信機（R1）"
        print(f"{name}: {b.port}  mac={doctor.mac_text(b.mac)}")
    print("組（相手の MAC と鍵）を消します。消した後は、無線の命令を受け付けません")
    if not confirm("消しますか", args.yes):
        print("やめました")
        return 1
    failed = 0
    for b in boards:
        reply = control.unpair(b)
        if reply.ok:
            print(f"{b.port}: 消しました（再起動します）")
        else:
            failed += 1
            print(f"{b.port}: 消せませんでした（reason={reply.reason}。ファームが古いと"
                  "この命令を知りません）", file=sys.stderr)
    print("組ませ直すには uv run csikit flash --pair-only")
    return 1 if failed else 0


def run_script(name: str, argv: list[str]) -> int:
    """scripts/ のスクリプトの main を呼ぶ.

    Raises:
        CliError: scripts/ が無い（リポジトリを clone せずに入れた場合）。
    """
    if not (SCRIPTS_DIR / f"{name}.py").is_file():
        raise CliError(f"{SCRIPTS_DIR / name}.py がありません。リポジトリを clone して、"
                       "その中で uv run csikit を使ってください")
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    module = importlib.import_module(name)
    code: int = module.main(argv)
    return code


def rx_port(args: argparse.Namespace) -> str:
    """受信機のポート（指定が無ければ探す）."""
    port: str = args.port or find_role("rx").port
    print(f"受信機（R1）: {port}", file=sys.stderr)
    return port


def cmd_monitor(args: argparse.Namespace) -> int:
    """受信のモニタ."""
    from csikit import monitor

    try:
        return monitor.run(rx_port(args), args.duration, args.save_figure)
    except monitor.MonitorError as e:
        raise CliError(str(e)) from e


def record_targets(args: argparse.Namespace) -> tuple[str, str | None]:
    """記録する受信機のポートと、見つかれば送信機のファームの版.

    ポートの指定が無ければ 1 回探して両方を見る。送信機が PC につながっていない・
    ポートを指定したときは、送信機の版は None（下書きでは ``unknown`` のまま）。
    """
    if args.port:
        print(f"受信機（R1）: {args.port}", file=sys.stderr)
        return args.port, None
    boards, silent = ports.find_boards(ports.list_ports())
    rx = require_role(boards, silent, "rx")
    print(f"受信機（R1）: {rx.port}", file=sys.stderr)
    tx = ports.pick_role(boards, "tx")
    if tx is None or tx.fw == meta.UNKNOWN:
        print("送信機の版は分かりません（PC につながっていない）。下書きでは unknown になります",
              file=sys.stderr)
        return rx.port, None
    print(f"送信機（T1）: {tx.port}  fw={tx.fw}（メタデータの下書きに入れます）", file=sys.stderr)
    return rx.port, tx.fw


def cmd_record(args: argparse.Namespace) -> int:
    """記録（既定はリセットしない）."""
    port, tx_fw = record_targets(args)
    extra = ["--duration", str(args.duration), "--label", args.label, "--port", port]
    if tx_fw is not None:
        extra += ["--tx-fw", tx_fw]
    if not args.reset:
        extra.append("--no-reset")
    return run_script("capture_csi", [*extra, *args.rest])


def build_parser() -> argparse.ArgumentParser:
    """引数の定義."""
    parser = argparse.ArgumentParser(prog="csikit", description="M5Stack Basic 2 台の CSI キット")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="点検する（足りないもの・直し方を表示）")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("flash", help="2 台に書き込み、組ませる")
    p.add_argument("--tx-port", help="送信機（T1）にするポート（既定: WHO で探す）")
    p.add_argument("--rx-port", help="受信機（R1）にするポート（既定: WHO で探す）")
    p.add_argument("--tx-bin", type=Path,
                   help=f"送信機の .bin（既定: {flash.default_image('tx')}）")
    p.add_argument("--rx-bin", type=Path,
                   help=f"受信機の .bin（既定: {flash.default_image('rx')}）")
    p.add_argument("--pair-only", action="store_true", help="書き込まずに組ませ直す")
    p.add_argument("--yes", action="store_true", help="書き込みの確認を聞かない")
    p.set_defaults(func=cmd_flash)

    p = sub.add_parser("monitor", help="受信のモニタ（振幅のヒートマップ・レート・欠け・RSSI）")
    p.add_argument("--port", help="受信機のポート（既定: WHO で探す）")
    p.add_argument("--duration", type=float, help="自動で閉じるまでの時間 [sec]")
    p.add_argument("--save-figure", help="閉じるときの画面を保存する（例: monitor.png）")
    p.set_defaults(func=cmd_monitor)

    p = sub.add_parser("record", help="記録（scripts/capture_csi.py）")
    p.add_argument("--port", help="受信機のポート（既定: WHO で探す）")
    p.add_argument("--duration", type=float, required=True, help="記録時間 [sec]")
    p.add_argument("--label", required=True, help="条件の名前（例: empty・wave）")
    p.add_argument("--reset", action="store_true",
                   help="記録の初めに受信機をリセットする（起動時の表示も記録に残る。"
                        "SD に記録中は使わない）")
    p.set_defaults(func=cmd_record, passthrough=True)  # 知らない引数はスクリプトにそのまま渡す

    p = sub.add_parser("channel", help="2 台のチャンネルを変える（受信機から送信機へ無線で伝える）")
    p.add_argument("channel", type=int,
                   help=f"チャンネル（{control.CHANNEL_MIN}〜{control.CHANNEL_MAX}。"
                        f"再起動すると {control.HOME_CHANNEL} に戻る）")
    p.add_argument("--port", help="受信機のポート（既定: WHO で探す）")
    p.set_defaults(func=cmd_channel)

    p = sub.add_parser("txpower", help="送信機の出力を変える（受信機から無線で伝える）")
    p.add_argument("dbm", type=float,
                   help=f"出力 [dBm]（{control.TX_POWER_MIN_DBM:g}〜{control.TX_POWER_MAX_DBM:g}）")
    p.add_argument("--port", help="受信機のポート（既定: WHO で探す）")
    p.set_defaults(func=cmd_txpower)

    p = sub.add_parser("unpair", help="つながっている M5Stack Basic の組（相手の MAC と鍵）を消す")
    p.add_argument("--yes", action="store_true", help="確認を聞かない")
    p.set_defaults(func=cmd_unpair)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """入口."""
    parser = build_parser()
    args, rest = parser.parse_known_args(argv)
    if rest and not getattr(args, "passthrough", False):
        parser.error(f"知らない引数: {' '.join(rest)}")
    args.rest = rest
    try:
        code: int = args.func(args)
    except (CliError, control.ControlError, pairing.PairError, flash.FlashError,
            FileNotFoundError, OSError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    return code


if __name__ == "__main__":
    sys.exit(main())
