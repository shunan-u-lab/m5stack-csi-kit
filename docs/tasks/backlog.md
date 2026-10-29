# m5stack-csi-kit — 後回しにしたこと

やらないと決めたわけではないが、いまはやらないもの。
`current.md` を短く保つための退避先。

書くときは**なぜ後回しにしたか**を添える。理由が無いものは、
後から見て「やる必要があったのか」を判断できない。

## 積んでいるもの

- 受信機が WHO に答えず「受信機が見つかりません」になった件の残り（2026-10-29）—
  起動中の受信機に送った WHO に答えてもらえない分は直した（起動の行を読んだら改行で区切って送り直す。
  REBOOT の直後に探して 0/5 → 5/5）。残るのは、前半の失敗で受信機が本当に再起動していたか・何が再起動させたか、
  1 回目の「答えなかったポート: なし」の理由、起動中の `nvs_open failed: NOT_FOUND` の行。再現しないので推測で直さない
  （`docs/test-log/2026-10-29-meta-firmware-rev-record.md`・`…-who-replug-and-reboot.md`・`…-who-resend-after-boot.md`）

- 受信機の Pico W 向けのコード（`TX2_MAC`・`TX_LIST`・`pico_*`・`PJOIN`/`PLEAVE`）を外す（2026-10-27）—
  見出しの `# tx id=` や `# stats` の欄（`matched2`・`frames2`）にまで入り込んでおり、外すと出力の形が変わる。
  実機で確かめられない間に外すと気づけないため、接続を既定で切る（`CSI_PICO_RELAY=0`）に留めた。
  実機で doctor → record が通るようになったら、外して PC 側のテストと記録の形を突き合わせる

- CI の actions を Node.js 24 対応の版に上げる（2026-10-27）— actions/cache@v4・upload-artifact@v4・setup-uv@v6 が Node.js 20 で、
  今は GitHub が Node.js 24 で動かしている（警告のみ）。版を推測で上げず、各 actions の配布元で新しい版を確かめてから上げる

- `scripts/sweep_channels.py` の既定のラベル・実験名（`exp05b`）と見出しの「実験05b」を、キット向けの名前にする（2026-10-28）—
  記録のファイル名とメタデータの `experiment` が変わり、テストも合わせて直す必要がある。動作には影響しないので後回し

## やらないと決めたもの

（決定を伴うものは `docs/decisions/` に ADR として書く）
