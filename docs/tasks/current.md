# m5stack-csi-kit — 進行中の作業

種類: プロジェクト

## いま何をしているか

公開した（2026-10-28・`v0.1.0`）。Windows・Mac の実機での確認と、機材の貸与元の先生方による確認を進める。
後回しにしているものは [backlog.md](backlog.md)。設計の判断は [docs/decisions/](../decisions/)。

## 再開メモ（2026-10-28 終わり）

- 公開・リリース・`main` の保護まで済んだ。作者の手元の後始末（フォルダの名前・運用の台帳）も済んだ。次に手を付けるのは下の「次の一手」の未完了の上から
- 実機（T1・R1）には `v0.1.0` とファームのソースが同じ版（`1028-917d081`）が入っている。書き込み直しは要らない
- `main` へは PR でしか入れられない。CI が通ってから squash でマージする。必須の検査は `ci-ok` 1 つ（PR #6）
- CI は変わったファイルで回すものを絞る（PR #6・`scripts/ci_scope.py`）。文書だけ → Python のテストを Linux だけ（約 1 分）、
  Python → 3 OS、ファーム・CI の設定・タグ → すべて（約 10 分）
- まっさらな Chromebook で見つかった `python3-tk` の抜けを、手順と `doctor` の「モニタの窓」に反映した（PR #4）。
  同じ試験で出た `live_csi.py` の太字の警告（IPA ゴシックに太字が無い）は、太字をやめて直した（PR #5）。
  どちらも `v0.1.0` には入っていない（次のリリースで入る）

## 次の一手

- [x] タグ `v0.1.0-rc.1` で CI（3 OS・約 9 分）が通り、下書きのリリース（.bin 2 つ・`SHA256SUMS`）ができた（2026-10-28。照合も OK）
- [x] ゼロからクローンして、Linux の実機で doctor → flash → monitor → record が通った（2026-10-28。
      [記録](../test-log/2026-10-28-fresh-clone-doctor-flash-monitor-record.md)。記録の中のコミット `917d081` は、公開前に履歴を整えたため今の履歴には無い）
- [x] public に切り替え、`v0.1.0` を公開した（2026-10-28）。直後に認証なしの `git clone` と、リリースの .bin の取得（照合 OK）、`doctor` が通った
- [x] CI を PR ごとの自動（3 OS・約 10 分）に戻し、`main` を保護した（2026-10-28。消す・強制 push の禁止、PR を必須、CI の 8 ジョブの成功を必須、管理者にも適用）
- [x] 作者の設定の無い環境で、AI と一緒に進めて迷わないかを確かめる（ADR 0008 の 4 段目）
      → 2026-10-28。まっさらな Chromebook に Claude Code を入れ、GitHub の URL から起動できた（初めに試した Codex CLI は操作に慣れず、Claude Code に切り替えた。Codex は未確認）。
      手順に無かった `python3-tk` が要った（無いと matplotlib が `agg` になり、`monitor` の窓が出ない）→ 手順に足し、`doctor` で検知するようにした（PR #4）。
      Lenovo Chromebook 100e（ARM）で、手順どおりの設定・ファームのビルド・書き込み・doctor・monitor・record・`live_csi.py` まで一式が動いた（[記録](../test-log/2026-10-28-fresh-chromebook-with-ai.md)）
- [ ] public からクローンして、Windows の実機で doctor → flash → monitor → record（`docs/test-log/` に記録）
- [ ] 同じく Mac の実機で
- [ ] 機材の貸与元の先生方に確認していただく
- [ ] CI を絞った後の最初の文書だけの PR で、`changes` が `python=light`・`firmware=false` を出し、`ci-ok` が通って約 1 分で終わるかを確かめる（PR #6 の後）
- [ ] 次のリリース（`v0.1.1`）を出すかを決める。中身: `doctor` の「モニタの窓」・`python3-tk` の手順（PR #4）、太字の警告の直し（PR #5）。
      ファームは変わっていないので、書き込み直しは要らない。Windows・Mac の確認の後にまとめてもよい。
      版を上げる PR では、AI が `CITATION.cff` の `version`・`date-released` も直す（AGENTS.md・PR #7）

## 待ち

（なし）
