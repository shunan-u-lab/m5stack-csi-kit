# 2026-10-28 キットのファームの書き込み・組ませ・署名（Linux 実機）

## 条件

- 機材: M5Stack Basic 2 台（CH9102）。T1（送信機）・R1（受信機）。前の試験（`2026-10-28-open-without-reset-doctor-monitor-record.md`）と同じ基板
- PC: ChromeOS の Linux（Crostini）、Python 3.14、esptool 5.3.1。ポートは `/dev/ttyACM0`（T1）・`/dev/ttyACM1`（R1）
- 書き込んだ .bin: `out/firmware/` の `csi-tx-m5stack-basic.bin`・`csi-rx-m5stack-basic.bin`（版 `1027-fc1b988+`。`+` は作業中の変更があるツリーから作った印）
- 2 台は机の上。距離・置き方は記録していない。MAC・鍵はここに書かない

## 1. 吸い出し（書き込む前）

`scripts/backup_flash.py` で 2 台のフラッシュ全体（16 MB）を `data/firmware-backup/` に保存（git の対象外）。
どちらもチップ側との照合が一致。R1 は 1 区間（0xC00000）で読み直しが 2 回あった後に読めた。1 台あたり約 3〜4 分。
吸い出しには受信機の Wi-Fi の設定（パスワードを含む）も入るので、ファイルの扱いに注意。

## 2. 書き込みと組ませ（`csikit flash --tx-port … --rx-port … --yes`）

| 項目 | 設計値 | 実測値 | 差 | 測り方 |
|---|---|---|---|---|
| 書き込み（送信機） | 照合一致 | 0x0 から 926,192 bytes・14.5 s・Hash 一致 | — | esptool の表示 |
| 書き込み（受信機） | 照合一致 | 0x0 から 1,068,528 bytes・16.5 s・Hash 一致 | — | esptool の表示 |
| 組ませ | 両方 `paired=1`、受信機の再起動は最大 40 s | 両方 `paired=1` | — | `flash` の表示・`doctor` |

`doctor` は全項目 OK（「問題はありません」）。

気づいたこと: 出力をパイプに流すと、「書き込むと元の中身は消えます」の注意が esptool の出力の**後**に出た（Python の標準出力のバッファ）。
端末で直接動かすときは順番どおりのはずだが、確かめていない。

## 3. monitor（20 秒）・record（20 秒）

| 項目 | 設計値 | 実測値 | 差 | 測り方 |
|---|---|---|---|---|
| 受信レート | 100 Hz | 99 Hz（monitor）・99.9 Hz（record） | −1〜−0.1 Hz | 画面・record の要約 |
| 欠け | 0 | monitor 0.0 %・record は連番の欠け 3（推定 6 行・2008 行中） | +3 | 画面・record の要約 |
| 受信機内の取りこぼし（`dropped`） | 0 | 0 | 0 | `# stats` |
| RSSI | — | −42 dBm 前後 | — | 画面 |

- record の先頭の `# who` に `paired=1`・`auth=on` が残った。記録中に受信機の再起動は無し
- 連番の欠け 3 は受信機の外（無線の区間）。受信機の中の取りこぼしは 0

## 4. 組と署名

| 試したこと | 期待 | 結果 |
|---|---|---|
| 受信機に `CH 6` → 送信機の `WHO` | 署名つきの指示が届き、2 台とも ch6 | `relay=ok acked=1 attempts=1`、送信機 `channel=6` |
| 受信機に `CH 11` | 2 台とも ch11 に戻る | `relay=ok acked=1`、送信機 `channel=11` |
| 送信機にだけ別の鍵（`PAIRSET`）→ 受信機に `CH 6` | 送信機が拒む | `# err cmd=CH reason=tx_no_ack attempts=3`。送信機は ch11 のまま、受信機も ch11 のまま |
| 受信機に `PAIRCLEAR` | 組が消え、`doctor` が NG | `paired=0`、`doctor`: 「組: 未設定（送信機 paired=1・受信機 paired=0）→ csikit flash --pair-only」 |
| `csikit flash --pair-only` | 組み直して元どおり | `doctor` 全項目 OK、`CH 6`/`CH 11` も再び通る |

- 鍵は画面・ファイルに出していない（試験のスクリプトは鍵を表示しない）
- 液晶の表示（`SENDING`・`RX from S`・`NOT PAIRED`）はこの試験では見ていない

## 次にどうするか

- 液晶の表示を目で確かめる（利用者）
- 書き込みで受信機の Wi-Fi の設定が消えたので、時刻を使うなら `sd_fetch.py wifi` で入れ直す
- 配布用の .bin は、作業中の変更が無いツリーから作り直す（版に `+` が付かないように）
- 元の研究のファームに戻すときは、吸い出した .bin を書き戻す


## 追記（2026-10-28・利用者から）: 置き方

T1 と R1 は **PC の両脇**に置いていた（PC を挟んで向かい合う形。間隔は PC の幅程度で、長さは測っていない）。
2 台を結ぶ線の上に PC 本体（金属・電池を含む）があり、まっすぐ届く電波の一部を遮っている可能性がある。
