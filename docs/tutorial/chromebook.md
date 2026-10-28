# 付録. Chromebook でゼロから始める

[目次](README.md) ・ 関係する章: [0. パソコンの基本](00_pc_basics.md)・[7. 困ったとき](07_troubleshooting_glossary.md)

Chromebook には、Chrome OS の中で Linux を動かす機能（**Linux 開発環境**）があります。
このキットは、その Linux の中で動かせます。この付録は、Chromebook を買ったままの状態から、`csikit doctor` が通るまでの手順です。

> **確かめた環境（2026-10-28）:** ARM の Chromebook、Linux 開発環境の Debian 13、M5Stack Basic 2 台（USB 変換チップ CH9102）。
> この環境で、書き込み・組ませ・モニタ（画面の表示を含む）・記録・ファームウェアのビルドまで通りました。
> Intel・AMD の Chromebook では試していません。設定の画面の名前は、Chrome OS の版によって少し違うことがあります。

🌱 **ファーストタッチ** — Chromebook の中に「もう 1 台の小さなパソコン」を置く

Chromebook は、ブラウザとアプリを使うためのパソコンで、そのままではプログラムを動かす道具を入れられません。
**Linux 開発環境**をオンにすると、Chromebook の中に、ターミナルで操作する小さな Linux のパソコンができます。
スマホの中に「仕事用のプロフィール」を作ると、普段のアプリと分けて仕事用のアプリを入れられるのに似ています。

- このキットの道具（uv・esptool など）は、すべてこの Linux の中に入れます。Chrome OS 側は汚しません
- USB でつないだ M5Stack は、最初は Chrome OS 側が持っています。**Linux 側に渡す**操作が要ります（手順 5）

---

## 手順

### 1. 使えるかを確かめる

- **学校・会社から貸し出された Chromebook は、管理者の設定で Linux 開発環境が使えないことがあります。** 設定に項目が無ければ、管理者に聞いてください
- 空き容量: Linux に **10 GB 以上**を割り当てるのを勧めます（推定。キットの部品で約 1 GB、ファームウェアを自分でビルドするなら道具でさらに約 3 GB。
  記録を長く取るとその分増えます）

### 2. Linux 開発環境をオンにする

1. 右下の時計 → 歯車（設定）を開く
2. 「詳細設定」→「デベロッパー」→「Linux 開発環境」の「オンにする」
3. ユーザー名とディスクのサイズ（手順 1 の 10 GB 以上）を決めて「インストール」。数分かかります
4. 終わると、黒い画面（**ターミナル**）が開きます。次からは、アプリの一覧の「ターミナル」→「penguin」で開けます

ターミナルの基本は [0-2](00_pc_basics.md#0-2-ターミナル--文字で頼むアプリ) にあります。

### 3. 最初に要るものを入れる

ターミナルに 1 行ずつ貼り付けて Enter を押します（ターミナルへの貼り付けは **Ctrl + Shift + V**）。

```sh
sudo apt update
sudo apt install -y git curl fonts-ipaexfont-gothic
```

- `sudo` は「管理者として実行する」という意味です。Linux 開発環境では、パスワードは聞かれません
- `git` はキットを手元に持ってくる道具、`curl` はインターネットからファイルを取ってくる道具です
- `fonts-ipaexfont-gothic` は日本語の文字の形（フォント）です。入れないと、モニタの画面の日本語が □ になります

### 4. uv を入れ、キットを持ってくる

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

入れたら、**ターミナルを一度閉じて、開き直します**。`uv --version` で版が出れば入っています（uv の説明は [0-3](00_pc_basics.md#0-3-uv--このキット専用の部屋を用意する係)）。

```sh
git clone https://github.com/shunan-u-lab/m5stack-csi-kit.git
cd m5stack-csi-kit
uv sync
uv tool install esptool
```

### 5. M5Stack を Linux 側に渡す

1. M5Stack を **1 台ずつ** USB でつなぎます（データ通信のできるケーブルで）
2. 画面の右下に「USB デバイスが検出されました」と出たら、「Linux に接続」を選びます
3. 出なかったとき・閉じてしまったときは、設定の「デベロッパー」→「Linux 開発環境」→「USB デバイスの管理」で、
   出てきた機器（`USB Single Serial` などの名前）をオンにします。2 台とも同じ名前に見えることがあります。両方オンにします
4. ターミナルで確かめます

```sh
ls /dev/ttyACM* /dev/ttyUSB*
```

`/dev/ttyACM0`（2 台なら `/dev/ttyACM1` も）が出れば、Linux 側に渡っています。何も出なければ、2・3 をもう一度。
`/dev/ttyUSB0` と出るのは、USB 変換チップが古い版（CP2104）の M5Stack です。どちらでも使えます。

### 6. ポートを使える集団に入っているかを確かめる

```sh
id | grep -o dialout
```

`dialout` と出れば大丈夫です（確かめた環境では、最初から入っていました）。何も出なければ:

```sh
sudo usermod -aG dialout "$USER"
```

と打ってから、**Linux を再起動**します（アプリの一覧の「ターミナル」を右クリック →「Linux をシャットダウン」→ もう一度ターミナルを開く）。

### 7. 点検する

```sh
uv run csikit doctor
```

ここから先は、README の「3. 点検する」以降と同じです。初めての M5Stack Basic は、README の「4. 書き込んで組ませる」へ進みます。

---

## Chromebook で気をつけること

| こと | 内容 |
|---|---|
| **USB を抜き差ししたら、渡し直しが要ることがある** **[起きた]** | 抜くと Linux 側から消えます。差し直したら、手順 5 の 2・3 で Linux に接続し直します。`doctor` の「USB のポートが見つからない」は、まずこれを疑います |
| 書き込み（`esptool`）の速さ **[起きた]** | Linux 開発環境では、921600 bps の読み出しで取りこぼしが多かったため、吸い出し（`scripts/backup_flash.py`）は 460800 bps にしてあります。`csikit flash` の書き込みも 460800 bps で通りました |
| ファイルを Chrome OS 側で見る | Linux の中のファイルは、Chrome OS の「ファイル」アプリの「Linux ファイル」に出ます。モニタの図（`--save-figure`）や記録は、そこから見られます |
| モニタの画面 | `uv run csikit monitor` のウィンドウは、Chrome OS の他のアプリと同じように開きます（確かめた環境で表示できました） |
| ファームウェアを自分でビルドする | 確かめた環境（ARM）で `pio run` が通りました。PlatformIO は `uv tool install platformio` で入ります。初回は道具の取得に時間と約 3 GB の容量がかかります |
| Linux の中身を消すと | 設定から Linux 開発環境をオフにすると、Linux の中のファイル（キット・記録を含む）は**全部消えます**。記録は先に Chrome OS 側か別の場所に写してください |

🌳 **もう一段** — Chromebook の中で、Linux はどう動いているの？

> この節は、**今は分からなくても先へ進んでかまいません**。

**まず、たとえで全体を見ます。** Chromebook の中に、**箱（仮想マシン）**が 1 つあり、その中にさらに**部屋（コンテナ）**があります。
ターミナルで操作しているのは、その部屋の中の Debian という Linux です。箱と部屋で Chrome OS から仕切っているので、
部屋の中で何を入れても Chrome OS は壊れません。その代わり、USB の機器やファイルは、仕切りを越えて**渡してもらう**必要があります。

**次に、自分の目で確かめます。** 部屋の中の Linux の名前と、CPU の種類を見ます。

```sh
grep PRETTY_NAME /etc/os-release
uname -m
```

`Debian GNU/Linux …` と、`aarch64`（ARM）か `x86_64`（Intel・AMD）が出ます。

**分かったこと**

- 手順 5 で USB を「Linux に接続」したのは、箱と部屋の仕切りを越えて、M5Stack を部屋に渡す操作だった
- `ls /dev/ttyACM*` で見えるのは、部屋に渡ってきた機器だけ

**今は分からなくてよい所:** 仮想マシンとコンテナの違い。Chrome OS の中の仕組みの名前（Crostini・Termina・penguin）。
