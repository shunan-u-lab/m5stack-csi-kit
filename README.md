[English](README.en.md) | 日本語

# M5Stack CSI Kit

[![Code: MIT](https://img.shields.io/badge/code-MIT-blue.svg)](LICENSE-CODE)
[![Docs: CC BY 4.0](https://img.shields.io/badge/docs-CC_BY_4.0-lightgrey.svg)](LICENSE-DOCS)

**M5Stack Basic 2 台と PC で、Wi-Fi の CSI（Channel State Information）を記録するキット**です。
Wi-Fi CSI を研究する学生が、M5Stack Basic 2 台・PC・USB ケーブル 2 本を用意すれば、ルーターやほかの機器なしで
「書き込み → モニタで確認 → 記録」まで迷わず進めることを目指しています。PC 側の道具は、ほぼすべて `uv` で入ります。

- 送信機（T1）が 100 Hz で電波を送り、受信機（R1）がその CSI を PC に流します（USB・921600 bps）。PC なしで受信機の SD カードに記録することもできます
- ルーターは要りません。2 台は決まったチャンネル（既定は ch11）で直接やりとりします
- 記録には、条件（配置・人数・区間）を書くメタデータの下書きが自動で付きます

> **マイコン・CSI・プログラミングが初めての人へ:** スマホのたとえから始める [チュートリアル](docs/tutorial/README.md) を先に読んでください。

> **おことわり:** 周南公立大学 大学院 情報科学研究科 第1期生（2026）吉田信文 が個人として公開している研究用の道具です。大学・研究室の公式なものではなく、専門家の確認も受けていません。**サポートは行いません。**
> 動作を確かめた構成は 1 つだけです（下の「動作確認の状況」）。[おことわり](DISCLAIMER.md)を先に読んでください。

## 用意するもの

| もの | 数 | メモ |
|---|---|---|
| M5Stack Basic | 2 | 送信機（T1）と受信機（R1）。**日本で使うなら技適マークのある個体**（下の「電波法」） |
| データ通信のできる USB ケーブル（USB-C） | 2 | 充電専用のケーブルでは PC から見えません |
| PC | 1 | Linux・Windows・Mac・Chromebook（Linux 開発環境）。Windows・Mac は実機で未確認 |
| microSD カード（FAT32） | 任意 | PC なしで記録するときだけ |

- **要らないもの:** Wi-Fi ルーター、ほかのマイコンの基板・アンテナ・配線、Arduino IDE などの開発環境（ファームを自分で変える人だけ PlatformIO が要ります）
- **`uv` の外で入れるもの:** Windows・Mac の USB ドライバ（要る場合だけ。下の 1.）

## はじめかた

### 1. 道具を入れる

[uv](https://docs.astral.sh/uv/) を入れてから:

```sh
git clone https://github.com/shunan-u-lab/m5stack-csi-kit.git
cd m5stack-csi-kit
uv sync                        # Python とライブラリ
uv tool install esptool        # 書き込みの道具
```

Windows・Mac では USB ドライバが要ることがあります（M5Stack Basic の USB 変換チップは、新しい版が CH9102、古い版が CP2104）。
Linux では、シリアルポートを使える集団（Debian・Ubuntu は `dialout`）に入ってからログインし直してください。
Debian・Ubuntu では、モニタの窓を出す部品も入れてください: `sudo apt install python3-tk`（無いと `csikit monitor` の窓が開きません）。
**Chromebook** は、Linux 開発環境をオンにし、M5Stack を Linux 側に渡す手順が要ります（[チュートリアルの付録](docs/tutorial/chromebook.md)。ARM の Chromebook で確認）。
足りないものは次の `doctor` が教えます。

### 2. ファームウェアを用意する

[Releases](https://github.com/shunan-u-lab/m5stack-csi-kit/releases) から `csi-tx-m5stack-basic.bin` と
`csi-rx-m5stack-basic.bin` を取り、`out/firmware/` に置きます（`SHA256SUMS` で確かめられます）。

自分でビルドする場合（ファームを変えたい人向け）:

```sh
uv tool install platformio
uv run python scripts/make_dist.py --build    # out/firmware/ に .bin を作る
```

### 3. 点検する

```sh
uv run csikit doctor
```

道具・ファーム・USB のポート・M5Stack Basic の応答・組の状態を順に見て、問題があれば直し方を表示します。

### 4. 書き込んで組ませる

まだこのキットのファームが入っていない M5Stack Basic は、どちらが送信機か分かりません。**1 台ずつ** USB につないで
`uv run csikit doctor` を実行し、ポート名（Linux は `/dev/ttyACM0`、Windows は `COM3` など）を控えてから:

```sh
uv run csikit flash --tx-port <T1 にするポート> --rx-port <R1 にするポート>
```

- **書き込むと、M5Stack Basic の元の中身（買ったときのデモなど）は消えます。** 残したい場合は先に `scripts/backup_flash.py` で吸い出してください
- **書き込み直すと、M5Stack Basic に保存した設定も消えます**（ファームの更新のときも同じ）。組は `flash` が入れ直しますが、
  受信機の時刻の取得に使う Wi-Fi（名前・パスワード）と画面の明るさは消えたままです。時刻も記録したい人は、書き込みの後に設定し直してください:
  `uv run python scripts/sd_fetch.py --port <R1 のポート> wifi --ssid <Wi-Fi の名前>`（パスワードは入力を求められます。画面には出ません）
- 書き込みの後、2 台を組ませます（相手の MAC と、組ごとに新しく作った鍵を、それぞれの M5Stack Basic に置きます）。受信機は再起動に最大 40 秒ほどかかります
- 2 回目からはポートを指定しなくても、`WHO` という命令で送信機・受信機を見分けます。組ませ直すだけなら `uv run csikit flash --pair-only`

うまくいくと、液晶の状態の帯が送信機は緑の `SENDING`、受信機は緑の `RX from T1` になります。

### 5. モニタで確かめる

```sh
uv run csikit monitor
```

上に振幅のヒートマップ（直近 10 秒）、下に RSSI、題名に受信レート（約 100 Hz）・欠け・取りこぼしを表示します。
手を振ると模様が乱れるのが見えるはずです。受信機はリセットしません。多機能のビューアは `scripts/live_csi.py` です。

### 6. 記録する

```sh
uv run csikit record --duration 60 --label empty
```

受信機をリセットせずに `data/raw/<日時>-<ラベル>.csv` に記録し（SD に記録中のセッションも切りません）、`data/meta/<同じ名前>.toml` にメタデータの下書きを書きます。
下書きの `TODO`（配置・人数・区間など）は、記憶が新しいうちに埋めてください（書き方: [docs/decisions/0003](docs/decisions/0003-recording-metadata-format.md)、
記入例: [examples/meta/](examples/meta/)）。

PC なしで記録するときは、受信機に SD カードを入れて、ボタン A で開始・B で停止・C で印を付けます。
取り込みは `uv run python scripts/sd_fetch.py`（[受信機の README](firmware/csi-rx/README.md) の「SD カードへの保存」）。

### 7. チャンネル・出力を変える、組を消す

```sh
uv run csikit channel 12      # 2 台のチャンネルを変える（1〜13。再起動すると 11 に戻る）
uv run csikit txpower 8       # 送信機の出力を変える [dBm]（2〜20。段階に丸められる。再起動すると既定に戻る）
uv run csikit unpair          # つながっている M5Stack Basic の組（相手の MAC と鍵）を消す（人に渡す前に）
```

チャンネルと出力は、受信機が署名つきの指示を送信機に無線で伝えます（送信機の USB は要りません）。
欠けが多いときはチャンネルを変えると減ることがあります（下の「動作確認の状況」のパケットロス）。記録の途中では変えないでください。

## 記録の形と読み込み

```python
from csikit import io

cap = io.load_rx("data/raw/20260101-120000-empty.csv")   # .csv.zst もそのまま読める
```

1 行の形式・列の意味は [受信機の README](firmware/csi-rx/README.md) の「出力」にあります。
長い記録は `uv run python scripts/raw_store.py compress` で約 1/5 に圧縮できます（[docs/decisions/0004](docs/decisions/0004-raw-storage-zstd-parquet.md)）。

## 倫理と安全

- **CSI は、壁越しでも人の在室や動きを映します。** 測られていることを知らない人を測らないでください。
  人を対象にする実験は、所属機関の研究倫理の手続き（倫理審査など）に従い、参加者の同意を得てください
- 記録は、在室や生活の様子を表す**個人に関する情報**になりえます。公開・共有する前に、何が読み取れるかを考えてください
- 周りの Wi-Fi のビーコンを調べる道具（`scripts/beacon_scan.py`）は、近所のアクセスポイントの名前（SSID）と識別子（BSSID）を表示します。
  画面や結果を共有するときは `--mask` で伏せてください
- 送信機は電波を送り続けます。記録しないときは電源を切ってください（周りの Wi-Fi への影響は測っていません）
- 医療・防犯・見守りなど、人の安全に関わる用途には使わないでください

### 電波法（日本で使う場合）

- **技適マーク（工事設計認証）のある M5Stack Basic を、改造せず**（アンテナの付け替えなど）に使ってください。
  認証の無い個体・海外向けの個体を日本で使うと、電波法に触れるおそれがあります
- ファームウェアは国の設定を日本（`JP`・ch1〜13）にしています。日本の外で使う場合は、その国の規則を自分で確かめてください

### 制御の署名

受信機から送信機への命令（チャンネル・出力の切り替え）には、組ごとの鍵で署名しています（[docs/decisions/0005](docs/decisions/0005-control-auth-and-tx-power.md)）。
鍵は M5Stack Basic の中（NVS・暗号化なし）にあるので、M5Stack Basic を人に渡す・返す前は組を消してください（2 台ともつないで `uv run csikit unpair`）。
守れるもの・守れないもの（電波から測られることは防げない）と自分でできることは、[チュートリアル 8 章](docs/tutorial/08_security.md) にたとえつきで書いています。

## 動作確認の状況

| 項目 | 状況 |
|---|---|
| ファームウェア（元の研究で M5Stack Basic 2 台・Linux） | 約 100 Hz・受信機内の取りこぼし 0 を確認 |
| `csikit doctor` | Linux の実機で、送信機・受信機の見分け、古いファーム・組の未設定の判定、組が揃えば「問題はありません」を確認（2026-10-28） |
| `csikit channel / txpower / unpair` | Linux の実機 2 台で、ch6 → ch11、出力 8 dBm 指定（実際 6.25 dBm）、2 台の組の消去 → `doctor` が未設定を案内 → `flash --pair-only` で元どおりを確認（2026-10-28） |
| 組を NVS に置く形（`PAIRSET`）・`csikit flash` | Linux の実機 2 台で、書き込み・組ませ・組み直し（`--pair-only`）・`PAIRCLEAR`、署名つきのチャンネル切替（鍵が違えば拒む）を確認（2026-10-28） |
| Chromebook（ARM・Linux 開発環境の Debian 13） | 上の `doctor`・`flash`・`monitor`・`record` は、すべてこの環境で確認（2026-10-28）。買ったままの別の ARM の Chromebook でも、付録の手順から一式が動いた（2026-10-28）。Intel・AMD の Chromebook は未確認 |
| Windows | 実機は**未確認**（CI のテストとファームのビルドは通過） |
| Mac | 実機は**未確認**（CI のテストとファームのビルドは通過） |
| パケットロス（無線の区間） | PC の両脇に 2 台を置き、各チャンネル 1 分（2026-10-28）: 全体 1.06 %、既定の ch11 は 0.32 %、チャンネルにより 0.08〜2.81 %。受信機の中の取りこぼしは 0。周りの Wi-Fi の混み具合で変わる（[記録](docs/test-log/2026-10-28-packet-loss-60s-per-channel.md)）。ch11 で送信出力を 19.5 → 0.75 dBm に下げてもロス率は 0.38〜1.11 % で増えなかった（[記録](docs/test-log/2026-10-28-tx-power-vs-loss-ch11.md)） |
| SD カードへの記録（受信機） | 元の研究のファームで、SD の CSI 行がシリアルの行と行数・中身・順番とも一致することを確認（[ADR 0006](docs/decisions/0006-sd-card-logging.md)）。このキットのファームでは**未確認** |
| `csikit monitor / record` | Linux の実機で、受信機をリセットせずに約 100 Hz で表示・記録できることを確認（2026-10-28。このキットのファームで 20 秒の記録の連番の欠けは 3） |
| CI（Linux・Windows・Mac） | テストとファームのビルドが通ることを確認（Mac は Apple Silicon の runner）。リリースのタグで 3 OS が自動で動く。それ以外は手動で動かす（Linux だけか 3 OS かを選ぶ） |

## リポジトリの中身

```
firmware/csi-tx/     送信機のファームウェア（命令の一覧は README）
firmware/csi-rx/     受信機のファームウェア（出力の形式・SD の使い方は README）
firmware/shared/     送受で共有する部品（署名・組・液晶・見張り）
src/csikit/          PC 側（csikit の命令・記録の読み込み・メタデータ）
scripts/             記録・取り込み・圧縮などの道具
examples/meta/       メタデータの雛形と記入例
docs/decisions/      設計の判断とその理由（ADR）
```

## ライセンス

- プログラム（`src/`・`tests/`・`scripts/`・`firmware/`・`examples/`）: **MIT** — [LICENSE-CODE](LICENSE-CODE)
- 文書（Markdown）: **CC BY 4.0** — [LICENSE-DOCS](LICENSE-DOCS)
- 概要: [LICENSE](LICENSE)
