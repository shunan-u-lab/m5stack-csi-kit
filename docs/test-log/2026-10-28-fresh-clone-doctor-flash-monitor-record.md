# 2026-10-28 公開リポをゼロからクローンして doctor → flash → monitor → record（v0.1.0-rc.1・Linux 実機）

## 条件

- 機材: M5Stack Basic 2 台（CH9102）。T1（送信機）・R1（受信機）。`2026-10-28-release-rc1-flash.md` と同じ基板
- PC: ARM の Chromebook の Linux 開発環境（Debian 13・aarch64）、uv 0.11.2、esptool 5.3.1。ポートは `/dev/ttyACM0`（T1）・`/dev/ttyACM1`（R1）
- 手順: 付録 `docs/tutorial/chromebook.md` の手順 4〜7 → README の「2. ファームウェアを用意する」以降。
  `git`・`curl`・フォント・uv・esptool は入っていたので、手順 3 と uv・esptool の導入は飛ばした
- キット: 空のフォルダにクローンした `main`（`917d081`）。.bin は下書きのリリース `v0.1.0-rc.1`（タグは同じ `917d081`）から取り、
  `sha256sum -c SHA256SUMS` で 2 つとも OK
- 書き込む前の基板の中身: 前のリリース候補のファーム（版 `1028-f107684`）・組ませた状態
- 2 台は机の上。距離・置き方は記録していない。MAC・鍵はここに書かない

## 1. クローン

リポがまだ private のため、付録どおりの `git clone https://github.com/shunan-u-lab/m5stack-csi-kit.git` は認証を求められて失敗した（想定どおり）。
`gh repo clone` で取った。**認証なしの `git clone` は public にした後に確かめる。**

## 2. doctor（.bin を置く前と後）

| 項目 | 設計値 | 実測値 | 差 | 測り方 |
|---|---|---|---|---|
| .bin を置く前 | .bin の 2 行が NG で、直し方を示す | 「無い」の NG 2 件と「リリースの .bin を out/firmware/ に置くか…」 | — | `doctor` |
| 2 台の見分け | WHO で送信機・受信機を判別 | `/dev/ttyACM0` → 送信機、`/dev/ttyACM1` → 受信機 | — | `doctor` |
| .bin を置いた後 | 全項目 OK | 「問題はありません。次は uv run csikit monitor」 | — | `doctor` |

## 3. 書き込みと組ませ（`csikit flash --yes`。ポート・.bin は指定しない）

| 項目 | 設計値 | 実測値 | 差 | 測り方 |
|---|---|---|---|---|
| 書き込み（送信機） | 照合一致 | 0x0 から 926,048 bytes・14.7 s・Hash 一致 | — | esptool の表示 |
| 書き込み（受信機） | 照合一致 | 0x0 から 1,068,320 bytes・16.6 s・Hash 一致 | — | esptool の表示 |
| 組ませ | 両方が互いを相手にする | 「組ませました」・`doctor` で「組: 送信機と受信機が互いを相手にしている」 | — | `flash`・`doctor` |
| 版 | `1028-917d081`（タグの版） | 2 台とも `fw=1028-917d081` | — | `doctor` |

「書き込むと元の中身は消えます」の注意が、esptool の出力より後に出た（出力をファイルに送ったとき。backlog の既知の問題と同じ）。

## 4. monitor（15 秒・`--save-figure`）

| 項目 | 設計値 | 実測値 | 差 | 測り方 |
|---|---|---|---|---|
| 受信レート | 100 Hz | 99 Hz | −1 Hz | 図の題名 |
| ロス | 0 % | 1.0 % | +1.0 % | 図の題名 |
| RSSI | — | −33 dBm | — | 図の題名 |
| 受信機内の取りこぼし（dropped） | 0 | 0 | 0 | 図の題名 |
| CRC 不良 | 0 | 2 行 | +2 | 図の題名 |
| 図に MAC が出ない | 出ない | 出ていない | — | 保存した図を目で確かめた |

ヒートマップと RSSI のグラフで時間軸の幅が揃っていない（backlog の既知の問題と同じ）。

## 5. record（10 秒・`--label empty`）

| 項目 | 設計値 | 実測値 | 差 | 測り方 |
|---|---|---|---|---|
| 受けた行 | 約 1,000 | 1,002 | — | record の要約 |
| 受信レート | 100 Hz | 99.6 Hz | −0.4 Hz | record の要約 |
| CRC 不良 | 0 | 1 行（0.10 %） | +1 | record の要約 |
| 連番の欠け | 0 | 4 | +4 | record の要約 |
| 記録が git に入らない | 入らない | `git status` に出ない | — | `git status` |

メタデータの下書き（TODO 16 件）もできた。

## 次にどうするか

- ゼロからクローンした状態で、付録と README の順に doctor → flash → monitor → record まで通った。手順の書き換えは要らない
- public にした直後に、認証なしの `git clone` から同じ手順をもう一度通す
- Windows・Mac は public にした後に確かめる
