# m5stack-csi-kit — M5Stack Basic 2 台で Wi-Fi CSI を記録する、他大学の学生向けの公開キット

作者の研究で安定した計測環境を、M5Stack Basic 2 台・PC・USB ケーブル 2 本（ルーター・ほかの機器なし。PC 側の道具はほぼ uv）で
「書き込み → モニタ → 記録」まで進められる形に切り出したもの。解析はサンプル程度に留める。
利用者向けの説明は `README.md`（日本語・メイン）・`README.en.md`（英語）、進行中の作業は `docs/tasks/current.md`。

## Commands

```sh
uv sync                                        # 依存
uv run pytest -q && uv run ruff check . && uv run mypy src   # テスト・lint・型
(cd firmware/csi-tx && pio run); (cd firmware/csi-rx && pio run)  # ファームのビルド（実機不要）
uv run python scripts/make_dist.py             # 書き込み用の .bin を out/firmware/ に作る
uv run csikit doctor                           # 実機の点検
uv run csikit channel 6 / txpower 8 / unpair    # 受信機経由で 2 台に命令（src/csikit/control.py）
```

## Architecture

```
firmware/         送信機（csi-tx）・受信機（csi-rx）・共有部品（shared）。命令と出力の形式は各 README
src/csikit/       PC 側。cli（入口）・ports（ポートと WHO）・pairing（PAIRSET）・control（CH・TXPS・PAIRCLEAR）・flash・doctor・io・meta ほか
scripts/          記録・取り込み・圧縮・配布用 .bin の作成
examples/meta/    メタデータの雛形と記入例（テストもここを読む）
examples/tutorial/ チュートリアルの図を作る実機なしの例
tests/            テスト（実機の代わりに偽の M5Stack Basic tests/fake_boards.py を使う）
docs/tutorial/    初学者向けのチュートリアル（ADR 0007）
docs/test-log/    実機試験の記録
docs/dependencies.md 使っている既存の機器・ソフトとその版
reference/        データシートなどの出典
docs/tasks/       進行中の作業（current.md）と後回し（backlog.md）
docs/decisions/   設計判断の記録（ADR・書き換えない）
```

## Watch out for（このリポは push される＝外に出る）

- **公開するキット。** 実機の MAC・BSSID・SSID・設置場所・記録（`data/`）を置かない。例はダミーの MAC（`02:00:00:00:00:xx`）を使う。記録をサンプルとして置くときは `scripts/mask_macs.py` で伏せた写しにする
- **鍵をどこにも出さない。** 古いファームは知らない命令の行をそのまま返す。PAIRSET の行・応答を表示・例外・ログに含めない
- **ポートは `ports.open_no_reset` で開く**（開いてから RTS → DTR の順に下げる）。開く前に `dtr = False` を決めておく形は Linux で毎回再起動させる。再起動すると受信機は NTP の待ちで最大約 30 秒黙る
- 実機で確かめていないことは README の「動作確認の状況」に「未確認」と書く
- **命令の名前・出力・`doctor` の文言を変えたら、`docs/tutorial/` も直す**（初学者向けのチュートリアルが手順と画面の見本を引用している。ADR 0007）
- **鍵・署名・合言葉・組の仕組みを変えたら、`docs/tutorial/08_security.md` と ADR 0005 も直す**（本文の数値の一部は `tests/test_tutorial_examples.py` で検査）
- **実在の連絡先・実名・人や場所に結びつく情報を書かない。** 記録・試験の記録・例にも入れない。例外は公開者本人の表示だけ（README・DISCLAIMER・LICENSE。ADR 0009）
- **鍵・パスワード（受信機の Wi-Fi など）は書かない。** 環境変数（`CSI_WIFI_SSID`・`CSI_WIFI_PASS`）か入力で渡す
- **既存の機器・ソフトを使ったら `docs/dependencies.md` に版を書く**
- **キットの版を上げるとき（`pyproject.toml` の `version`）は、AI が同じ PR で `CITATION.cff` の `version` と `date-released` も書き直す。**
  `date-released` はタグを打つ予定の日（決まっていなければ PR を作る日）。人が手で直す前提にしない。
  版の一致は `tests/test_public_docs.py` が検査するが、日付は検査しない
- **実機で試したら `docs/test-log/` に記録する**（設計値・実測値・差。MAC・SSID は書かない）

## 学ぶ人と作業するとき（ADR 0008）

このキットを使う人の多くは、動かしながら仕組みを学んでいる学生。AI は近道ではなく**説明役**として振る舞う。

- **実行する前に**、何をするか・なぜかを 1〜2 文で言う。**終わったら**、何が起きたかと、それを担当するファイル・関数を示す
  （例: 「`doctor` の『組』の行は `src/csikit/doctor.py` が `WHO` の `paired=` を読んで出している」）
- 対応するチュートリアルの節を示す（例: 「これは `docs/tutorial/02_connect_boards.md` の doctor の読み方」）。命令の意味が分からないと言われたら、まずその章を案内する
- **次の操作は、本人の確認を取ってから行う**（元に戻せない・基板の中身が変わる）:
  `csikit flash`（書き込み。元の中身と Wi-Fi の設定が消える）・`csikit unpair`（組を消す。`PAIRCLEAR`）・
  組ませ直し・`scripts/sd_fetch.py rm`（SD のファイルを消す）・`scripts/sd_fetch.py wifi`
- 本人が「自分で打ちたい」「教えて」と言ったら、答えを先に全部書かず、本人が打てる 1 行の命令と、見るべき表示を示す
- 鍵・`PAIRSET` の行・実機の MAC・周りの SSID/BSSID・Wi-Fi のパスワードを、画面への出力・ファイル・コミットに出さない。
  コマンドの出力に MAC が出るときは伏せて見せる。記録（`data/`）はコミットしない（共有するなら `scripts/mask_macs.py` の写し）
- 失敗したら、7 章（`docs/tutorial/07_troubleshooting_glossary.md`）の表で同じ症状を探し、そこに書いた順で確かめる。
  表に無い症状なら、推測で直さず、何を確かめたかと次の一手を本人に示す

## 作者の環境について

このリポは作者の Claude Code で作ってきた。作者の PC だけにある設定（`CLAUDE.local.md`・`.githooks/`）は git に入っていない。
ここに書いていない道具・ファイル・リポジトリを前提にしない。
