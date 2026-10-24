# 検証記録を読む

[検証戦略](strategy.md)は手順と期待結果、results配下は特定の版で実施した結果です。結果には日時、コードのコミットと内容ハッシュ、環境、検査名、期待結果との差、未確認事項を記載します。実行時のファイルや個人情報は.artifacts/recordsへ保存します。

| 記録 | 対象 |
|---|---|
| [物語計画・作品保存・公開API](results/story-workflow.md) | 自動枚数、比較の成否、作品移行、Python API、T4の接続・生成・終了 |
| [ストーリー生成](results/story-generation.md) | Fake・4B・T4による計画と順次保存、残る品質・起動の課題 |
| [品質ゲート・保存・評価](results/quality-storage-evaluations.md) | 累積検査、保存競合、AI向け評価記録とT4の生成・終了 |
| [準備・保存機能](results/seamless-baseline.md) | 保存構成の再編前の実機結果 |
| [統合時の保存保護](results/merge-integration.md) | 使用中の仮想環境・キャッシュ・一時ファイルの保護 |
| [保存・セット・操作の再編](results/repository-rebuild.md) | 今回の変更の検証範囲と環境上の制約 |

最新成功ポインターは全工程の成功後だけ更新します。全体が失敗した実行の部分成功は、成功ポインターへ昇格させません。CIとローカルの結果は、同じコミット・コードハッシュであることを確認して参照します。
