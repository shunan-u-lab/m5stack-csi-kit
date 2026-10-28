# 0001. 対象は M5Stack Basic 2 台・Linux と Windows の実機確認・uv だけで動く形にする

- ステータス: **採用**（2026-10-27）
- 対象: キット全体（firmware/・src/csikit/・scripts/・README）

## 背景

元の研究リポジトリでは、ESP32 DevKit 2 台・Raspberry Pi Pico W・M5Stack Basic 2 台の 3 系統で CSI を取ってきた。
キットの利用者は他の大学の学生で、研究室か自宅の PC（Linux・Windows・Mac のどれか）を使う。
問い合わせに答える体制は無い（サポートは行わない）ので、「動いた構成をそのまま再現できる」ことを優先する必要がある。

選択肢:

1. 3 系統すべてに対応する
2. M5Stack Basic だけに絞る
3. ESP32 DevKit だけに絞る（安い）

道具の入れ方の選択肢:

- A. Arduino IDE・PlatformIO の拡張など、利用者に任せる
- B. uv を土台にし、PlatformIO・esptool も `uv tool` で入れる

## 決定

- 対象機種は **M5Stack Basic のみ**（送信機 S・受信機 R の 2 台）。DevKit・Pico W 向けの分岐はキットから外す
- OS は **Linux・Windows を実機で確認**し、**Mac は CI のビルドとテストのみ**。README に「Mac の実機は未確認」と書く
- 土台は **uv**。Python の依存は `uv.lock`、PlatformIO・esptool は `uv tool` で入れる。
  標準の書き込みは**ビルド済みの `.bin` を esptool で書く**方法にし、PlatformIO はファームを変えたい人向けにする

## 根拠

- M5Stack Basic は液晶・ボタン・SD スロット・電池を持つため、PC なしの記録（SD）と、その場での確認（液晶）が 1 台で済む。
  DevKit は安いが、この 2 つを足すには配線が要り、構成がばらつく
- 元の研究で、M5Stack Basic 2 台の組で受信レート約 100 Hz・欠け 0（`# stats rate_hz≈100 dropped=0`）を確認している
- 系統を増やすと、確認しなければならない組み合わせが掛け算で増える。サポートしない以上、確認済みの構成は 1 つに保つ
- uv の外に残るのは Windows・Mac の USB ドライバ（CH9102）だけの見込み。手順の分岐が最小になる
- ビルド済み `.bin` を標準にすると、Apple Silicon で espressif32@6.9.0 のコンパイラが Rosetta 2 を要るかもしれない問題
  （未確認）を利用者が踏まずに済む
- Mac の実機は手元に無い。確認していないものを「動く」と書かない
