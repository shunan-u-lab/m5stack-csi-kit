# 2026-10-28 まっさらな Chromebook で、AI と一緒に Linux 開発環境 → ビルド → 書き込み（ADR 0008 の 4 段目）

## 条件

- PC: Lenovo Chromebook 100e（型番 82W0000FJP）。ARM（`uname -m` は `aarch64`）。買ったままの状態から始めた
- 手順: 付録 `docs/tutorial/chromebook.md` の手順 1〜7。Claude Code を入れ、GitHub の URL から作業を始めた（作者の設定は無い）
- キット: `main`（`v0.1.0` の公開後。PR #4 の `python3-tk` の手順は、この試験で見つかって後から足した）
- 機材: M5Stack Basic 2 台。基板の個体・USB 変換チップ・ポート名は記録していない
- 記録していないもの: Linux 開発環境の Debian の版、uv・esptool・PlatformIO の版、所要時間

## 結果

| 項目 | 設計値 | 実測値 | 差 | 測り方 |
|---|---|---|---|---|
| Linux 開発環境の設定 | 付録の手順どおりに進む | 手順どおりに設定できた | — | 利用者の報告 |
| モニタの窓（matplotlib の描画先） | 窓を出せる描画先（`tkagg` など） | 初めは `agg`（画面なし）。`python3-tk` を入れて解消 | 手順に `python3-tk` が無かった | 利用者の報告 |
| ファームウェアのビルド（`pio run`） | 通る | 通った | — | 利用者の報告 |
| 書き込み | 通る | 通った | — | 利用者の報告 |
| `doctor`（「モニタの窓」の行を含む） | 全項目 OK | 未確認 | — | — |
| `monitor`（窓が出るか） | 窓が出る | 未確認 | — | — |
| `record` | 記録できる | 未確認 | — | — |

**`python3-tk` が要った理由:** uv が Linux 開発環境の Debian の Python 3.13（`/usr/bin/python3.13`）を使った。
Debian は Python の tkinter を別のパッケージ `python3-tk` に分けているため、matplotlib が窓を出す部品を見つけられず、`agg` を選んだ。
作者の Chromebook には `python3-tk` が入っていたため、これまでの試験では表に出なかった。

## 次にどうするか

- 手順を直した（PR #4）: 付録の手順 3 の apt の行・README（日・英）・7 章に `python3-tk` を足した
- 検知できるようにした（PR #4）: `doctor` に「モニタの窓」の行を足した。`monitor` も、窓を出せないときは理由を言って止まる
- `doctor`・`monitor`・`record` をこの PC で通し、別の記録に残す。Intel・AMD の Chromebook は、まだ未確認のまま
