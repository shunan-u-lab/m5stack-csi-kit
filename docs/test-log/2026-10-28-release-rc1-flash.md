# 2026-10-28 CI で作ったリリースの .bin を実機に書き込む（v0.1.0-rc.1・Linux 実機）

## 条件

- 機材: M5Stack Basic 2 台（CH9102）。T1（送信機）・R1（受信機）。`2026-10-28-kit-firmware-flash-and-pair.md` と同じ基板
- PC: ChromeOS の Linux（Crostini）、esptool 5.3.1。ポートは `/dev/ttyACM0`（T1）・`/dev/ttyACM1`（R1）
- 書き込んだ .bin: 下書きのリリース `v0.1.0-rc.1`（タグは `main` の `f107684`）を `gh release download` で取ったもの。
  `sha256sum -c SHA256SUMS` で 2 つとも OK。書き込む前の基板の中身はキットのファーム（版 `1028-a74b527`）
- 2 台は机の上。距離・置き方は記録していない。MAC・鍵はここに書かない

## 1. USB の渡し直し

T1 を USB でつなぎ直したところ、Chromebook の Linux に渡っておらず `/dev/ttyACM0` が無かった。
Chrome OS 側で Linux に接続し直すと `/dev/ttyACM0`・`/dev/ttyACM1` の 2 つが見えた。
付録 `docs/tutorial/chromebook.md` の「抜き差ししたら渡し直しが要ることがある」は、実際に起きた。

## 2. 書き込みと組ませ（`csikit flash --yes --tx-bin … --rx-bin …`。ポートは指定しない）

| 項目 | 設計値 | 実測値 | 差 | 測り方 |
|---|---|---|---|---|
| 2 台の見分け | WHO で送信機・受信機を判別 | `/dev/ttyACM0` → 送信機、`/dev/ttyACM1` → 受信機 | — | `flash` の表示 |
| 書き込み（送信機） | 照合一致 | 0x0 から 926,048 bytes・14.5 s・Hash 一致 | — | esptool の表示 |
| 書き込み（受信機） | 照合一致 | 0x0 から 1,068,320 bytes・16.8 s・Hash 一致 | — | esptool の表示 |
| 組ませ | 両方が互いを相手にする | 「組ませました」・`doctor` で「組: 送信機と受信機が互いを相手にしている」 | — | `flash`・`doctor` |
| 版 | `1028-f107684`（タグの版） | 2 台とも `fw=1028-f107684` | — | `doctor` |

`doctor` は全項目 OK（「問題はありません」）。

## 3. record（10 秒）

| 項目 | 設計値 | 実測値 | 差 | 測り方 |
|---|---|---|---|---|
| 受信レート | 100 Hz | 99.5 Hz | −0.5 Hz | record の要約 |
| CRC 不良 | 0 | 1 行（0.10 %） | +1 | record の要約 |
| 連番の欠け | 0 | 5 | +5 | record の要約 |

monitor はこの回は動かしていない（キットのファームでは前の試験で確かめた）。

## 次にどうするか

- CI で作ったリリースの .bin は、手元で作った .bin と同じように書き込み・組ませ・記録まで通る。リリースの仕組みはこのまま使う
- 書き込みで R1 の時刻用の Wi-Fi の設定が消える（README・7 章に書いたとおり）。`sd_fetch.py wifi` で入れ直す
- Windows・Mac でリリースの .bin を書き込むのは未確認
