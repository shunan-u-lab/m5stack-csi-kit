# m5stack-csi-kit — 進行中の作業

種類: プロジェクト

## いま何をしているか

公開した（2026-10-28・`v0.1.0`）。Windows・Mac の実機での確認と、機材の貸与元の先生方による確認を進める。
後回しにしているものは [backlog.md](backlog.md)。設計の判断は [docs/decisions/](../decisions/)。

## 次の一手

- [x] タグ `v0.1.0-rc.1` で CI（3 OS・約 9 分）が通り、下書きのリリース（.bin 2 つ・`SHA256SUMS`）ができた（2026-10-28。照合も OK）
- [x] ゼロからクローンして、Linux の実機で doctor → flash → monitor → record が通った（2026-10-28。
      [記録](../test-log/2026-10-28-fresh-clone-doctor-flash-monitor-record.md)。記録の中のコミット `917d081` は、公開前に履歴を整えたため今の履歴には無い）
- [x] public に切り替え、`v0.1.0` を公開した（2026-10-28）。直後に認証なしの `git clone` と、リリースの .bin の取得（照合 OK）、`doctor` が通った
- [ ] CI を PR ごとの自動（3 OS）に戻し、`main` を保護する（消す・強制 push の禁止、PR を必須、CI の成功を必須）
- [ ] 作者の設定の無い環境で、AI と一緒に進めて迷わないかを確かめる（ADR 0008 の 4 段目）
- [ ] public からクローンして、Windows の実機で doctor → flash → monitor → record（`docs/test-log/` に記録）
- [ ] 同じく Mac の実機で
- [ ] 機材の貸与元の先生方に確認していただく

## 待ち

（なし）
