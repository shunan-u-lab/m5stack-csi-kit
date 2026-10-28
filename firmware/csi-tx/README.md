# csi-tx — CSI 送信機（M5Stack Basic）

ESP-NOW のブロードキャストを **100 Hz・802.11n MCS0/HT20 固定**で送り続ける。チャンネルは起動時 ch11 で、
シリアルの命令か受信機からの無線の命令で ch1〜13 に切り替えられる。
受信機（`firmware/csi-rx`）はこのパケットから CSI を得る。ESP32 は自分が受信したパケットからしか CSI を
取れないため、受信側のサンプリングレートはここで決まる。

## 書き込みと組の設定

普段は `csikit flash` を使う（ビルド済みの `.bin` を書き、受信機と組ませる）。ファームを変えたいときだけ自分でビルドする:

```sh
uv tool install platformio          # 初回のみ
cd firmware/csi-tx
pio run                             # ビルド（実機不要）
pio run -t upload --upload-port <ポート>
```

書き込んだだけでは**組（受信機の MAC と制御の鍵）が未設定**で、液晶に黄色の `NOT PAIRED`、起動時の表示に
`paired=0` と `[tx] not_paired` が出る。この状態でも送信は続けるが、受信機からの無線の命令は受け付けない。
組はシリアルの `PAIRSET` で NVS に置く（`csikit flash` が両方の M5Stack Basic に送る。判断は `docs/decisions/0002`）。

## 正常時の出力（115200 bps）

```
[tx] CSI transmitter v4 commands=CH,WHO,TXP,WDTTEST,REBOOT,PAIRSET,PAIRCLEAR relay=1 watchdog_ms=4000 led_pin=2 auth=on paired=1 rx_mac=020000000002 tx_power_dbm=19.50 fw=1027-1a2b3c4 boot=poweron
[tx] mac=020000000001 channel=11 (want 11) bw=HT20 interval_ms=10 rate=MCS0_LGI country=JP
[tx] sending
[tx] seq=100 rate_hz=100.0 cb_ok=99 cb_fail=0 send_err=0 last_err=ESP_OK channel=11 ctrl_rx=0 ctrl_bad=0
```

- `rate_hz` が 100 から外れる、`send_err` が増える → 送信が詰まっている
- 初期化に失敗すると `[tx] FATAL: <API> failed: <理由>` を 2 秒ごとに出して止まる
  （黙って送らないまま動き続けることはない）

## シリアルの命令

115200 bps で 1 行ずつ送る（改行 `\n` で区切る。最大 64 文字）。

| 命令 | 応答（成功） | 内容 |
|---|---|---|
| `WHO` | `[tx] who role=tx mac=<MAC> channel=<n> auth=<on/off_no_key> tx_power_dbm=<dBm> fw=<版> paired=<0/1> rx_mac=<受信機の MAC>` | 再起動せずに、この M5Stack Basic かどうかと今の設定を確かめる |
| `PAIRSET <受信機の MAC 12 桁> <鍵 32 桁>` | `[tx] ack cmd=PAIRSET restarting` | 組を NVS に置いて再起動する。応答に鍵は出さない |
| `PAIRCLEAR` | `[tx] ack cmd=PAIRCLEAR restarting` | 組を消して再起動する（機材を返す・人に渡す前） |
| `CH <1〜13>` | `[tx] ack cmd=CH channel=<n>` | 送信チャンネルを切り替え、読み戻して確かめる |
| `TXP <2〜20>` | `[tx] ack cmd=TXP want_dbm=<指定> dbm=<読み戻し>` | 送信出力の上限 [dBm] を変える |
| `REBOOT` | `[tx] ack cmd=REBOOT restarting` | 再起動する（次の起動の表示は `boot=sw`） |
| `WDTTEST` | `[tx] wdt_test block_ms=…` | わざと loop を止めてウォッチドッグを確かめる（5 秒ほどで再起動する） |

- 失敗は `[tx] err cmd=<命令> reason=<理由>`（`out_of_range` / `verify_failed` / `bad_mac` / `bad_key` /
  `nvs_open` / `nvs_write` / `unknown` / `too_long`）。送信は止めない
- 起動時は `WIFI_CHANNEL`（ch11）。切り替えたチャンネルは電源を切ると戻る
- 国は `"JP"` に明示する（日本では ch1〜13 が OFDM で使える。ch14 は DSSS のみで 11n は不可）

## 受信機からの無線の命令

送信機を PC から離して**電源だけ**で使えるように、組んだ受信機から ESP-NOW で届く制御パケットでも
チャンネル・出力を変え、再起動する。PC は受信機に命令を送るだけでよい（受信機の README）。

| 受け取る制御パケット | 動作 |
|---|---|
| `SET_CH n` | 了解（`ACK`。範囲外は `NACK`）を**今のチャンネルで 3 回**ブロードキャストしてから切り替える |
| `KEEPALIVE` | 受信機が 1 秒ごとに送る合図。番犬の時計を戻す |
| `SET_TXP` / `TXP_CONFIRM` | 出力を試しに変え、確認が来たら確定する（下の「受信機から出力を変える」） |
| `REBOOT` | 了解を返してから再起動する |

- **番犬**: 起動時のチャンネル（ch11）以外にいるとき、受信機の合図が 4 秒（`WATCHDOG_MS`）途絶えたら ch11 に戻る
  （`[tx] watchdog no_control_ms=... back_to=11`）。受信機も同様に戻るので、見失っても両方が ch11 で出会う
- 了解を 3 回送るのは、ブロードキャストは再送されないため（1 回だけだと 13 回中 1 回届かなかった。3 回にして 26 回中 0 回）
- 組んだ受信機（`rx_mac`）以外からの制御パケット、署名・合言葉の合わない制御パケットは無視し、`ctrl_bad` に数える

### 署名

ESP-NOW のブロードキャストは暗号化できない（ESP-IDF の ESP-NOW の説明）。そのままでは送り元の MAC を偽れば
誰でも制御パケットを作れるため、組の鍵で署名する（判断は `docs/decisions/0005`）。

- **署名**: HMAC-SHA256 の先頭 8 バイト（`firmware/shared/CsiAuth`）。送信機はデータのパケットに合言葉
  （`ctrl_nonce`・起動ごとの乱数）を載せ、受信機はそれを含めて署名する。送信機は命令を受け入れるたびに合言葉を
  変えるので、録音した命令の流し直しは通らない
- **鍵**: 16 バイト。`csikit flash` が組ごとに乱数で作り、`PAIRSET` で両方の NVS に置く。配る `.bin` には入らない
- 防げないこと: 中身の傍受（平文）、送っている電波から第三者が CSI を測ること。記録しない時は電源を切る

### 受信機から出力を変える

受信機のシリアルで `TXPS <2〜20>` を送ると、送信機の出力を無線で変える（署名付きの SET_TXP）。

1. 送信機は出力を変え、変えた出力で了解（実際の値）を返し、合言葉を変えて「試し」に入る（液晶に `TRIAL`）
2. 受信機は、新しい出力の電波（新しい合言葉の載ったパケット）が届くのを確かめてから確認を返す →
   `# ack cmd=TXPS want_dbm=<指定> dbm=<実際の値>`
3. 送信機は 3 秒（`TXP_CONFIRM_MS`）以内に確認が来なければ、自分で元の出力に戻る（`[tx] txp revert`）。
   受信機は `# err cmd=TXPS reason=lost_link reverted=<1/0>`

送信機の今の出力はデータのパケット（`tx_qdbm`）に載り、受信機の液晶と `# stats` の `peer_tx_power_dbm=` に出る。
**記録ファイルにも毎秒残る**。

**記録の途中で出力を変えない。** 空間が変わらなくても、出力を変えるだけで正規化した振幅の形が
人の小さな動きと同じ程度以上に変わる（距離 5 cm で 0.031〜0.045）。出力は記録に残るので解析で切り替わりを
見つけることはできるが、TXP・TXPS は記録の前後だけで使う。

実測（M5Stack Basic 2 台・同じ机の上）:
- `TXPS 8` → 6.50 dBm で確定。`TXPS 19.5`・`TXPS 20` とも 17.50 dBm（命令で設定できる上限は 17.50 のようで、
  起動時の既定 19.50 には戻らない。理由は未確認。既定に戻すには送信機を再起動する）
- 出力を 19.5〜2 dBm に変えて各 15 秒記録: RSSI −17〜−39 dBm・欠番 0.1〜0.4%。2 台が近すぎて、
  出力と RSSI の関係は単調でなかった。読み戻しは 2 dBm 指定で 0.00（理由は不明）。2026-10-28 のキットのファームでは 2 dBm 指定で 0.75（`docs/test-log/2026-10-28-tx-power-vs-loss-ch11.md`）。実際の配置で下げられる下限は未測定

## 見張りと再起動

| 手段 | 使い方 | 次の起動の表示 |
|---|---|---|
| ウォッチドッグ（自動） | loop が 5 秒止まると再起動する（Arduino-ESP32 の `CONFIG_ESP_TASK_WDT_TIMEOUT_S=5`・`PANIC=y`。`firmware/shared/CsiBoot`） | `boot=task_wdt` |
| 受信機から（無線・署名付き） | 受信機で `TXREBOOT` | `boot=sw` |
| USB | `REBOOT` | `boot=sw` |

**記録の途中で再起動しない**: 連番が 0 に戻る。受信機は `# stats` の `tx_restarts` に数えるが、
再起動の間（約 1.6 秒）のデータは抜ける。

## 液晶の表示

描画は別タスク（コア 0・1 秒ごと。`firmware/shared/CsiLcd`・M5GFX 0.1.17）で行い、送信ループ（100 Hz）は
内容をキューに置くだけで待たない（液晶を足しても 100.0 Hz・`send_err=0` を確認）。

| 行 | 内容 |
|---|---|
| 上の帯 | 機器番号（`T1`）と、役割・ファームウェアの版（右の上下 2 段） |
| 状態の帯 | 緑 `SENDING` / 黄 `NOT PAIRED`（組が未設定）・`LOW RATE`（90 Hz 未満）・`NO R1`（受信機の合図が 3 秒ない）/ 赤 `SEND ERROR`（直近 1 秒に送信エラー） |
| 本文 | MAC・チャンネルと送信レート・送信出力（試し中は `TRIAL`）・連番と送信エラー数・受信機の合図からの経過秒・起動からの時間 |

- **画面の明るさ**: ボタン C（右）を 1 秒押し続けると 普通 → 暗い → 消灯 → 普通。NVS に覚えるので、
  消灯にすれば電源を入れ直しても暗いまま。消灯中は、どのボタンも最初の 1 回は画面を点けるだけ
- 表示は ASCII のみ（日本語フォントはプログラム領域を大きく使うため）
- **ファームウェアの版**: `fw 1027-1a2b3c4` の形（ビルドした月日-git の短いハッシュ）。`firmware/` の下に
  コミットしていない変更があるビルドは末尾に `+` が付く。`firmware/shared/fw_version.py` が作り、シリアルの
  起動時の表示と `WHO` の `fw=` にも出る

## ペイロード（リトルエンディアン・22 バイト・版 4）

| オフセット | 型 | 内容 |
|---|---|---|
| 0 | uint32 | magic `0x54495343`（"CSIT"） |
| 4 | uint16 | 版（4） |
| 6 | uint16 | 送信間隔 [ms] |
| 8 | uint32 | 連番（0 始まり） |
| 12 | uint32 | 送信時刻 [ms]（送信機の起動からの millis） |
| 16 | uint32 | 制御パケットの合言葉 `ctrl_nonce` |
| 20 | int8 | 今の送信出力の上限 [0.25 dBm] `tx_qdbm` |
| 21 | int8 | チップの温度 [℃] `tx_temp_c`（内蔵センサーの目安で室温ではない。1 秒ごとに読み直す） |

制御パケット（版 2・24 バイト）: magic `0x43495343`（"CSIC"）・版・命令・チャンネル・token・nonce（各 4 / 2 / 1 / 1 / 4 / 4 バイト）
＋署名 8 バイト（先頭 16 バイトの HMAC-SHA256）。

## 受信側と揃えるもの

- **チャンネル**: `WIFI_CHANNEL`（ch11）。切り替えは受信機の `CH` で両方を動かす
- **組**: 送信機には受信機の MAC、受信機には送信機の MAC を、同じ鍵とともに置く（`csikit flash`）
