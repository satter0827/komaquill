# 検証記録を読む

[検証戦略](strategy.md)は手順と期待結果、results配下は特定の版で実施した結果です。結果には日時、コードのコミットと内容ハッシュ、環境、検査名、期待結果との差、未確認事項を記載します。実行時のファイルや個人情報は.artifacts/recordsへ保存します。

| 記録 | 対象 |
|---|---|
| [通常環境のKlein確認と容量整理](results/klein-readiness.md) | 通常の起動経路、生成物の見直し、標準画面、LoRA比較、個別データの削除 |
| [Kleinによる短編漫画](results/klein-completion.md) | 指定モデル、4場面の生成、評価と組版、期限による回収と終了 |
| [4場面の保存完了と改善比較](results/story-completion.md) | 基本8枚・改善4枚、仮選択と採否、残る品質、専用T4の終了 |
| [4場面の完走手順](story-completion.md) | 保存済み計画、仮選択と採否、出力・時間上限、限定的な改善 |
| [前景合成の素材確認](results/foreground-composition.md) | 素材12枚の不採用、共通条件・評価・保存時刻の契約、比較と受入の未実施範囲 |
| [段階生成・素材入力](results/staged-workflows.md) | 3系統の実験用ワークフロー、素材・マスク、失敗出力、T4比較と受入の制約 |
| [複数候補・正負入力・場面実行](results/batch-candidates.md) | 標準バッチ、正負のファイル入力、最大2要求の送信、T4の25画像、速度と選択の制約 |
| [4B・9Bの切替と用途別比較](results/llm-comparison.md) | CPU動作・計画12条件・既存8画像・時間切れとメモリ・元の設定への復元 |
| [場面入力・再開・LLM修正の再検証](results/story-image-review.md) | 再開ハッシュと入力検証、修正会話、T4の4場面・両画面・通常終了、品質の未達 |
| [場面入力・品質プロンプト・領域指定](results/story-image-inputs.md) | 新しい場面契約、固定入力、4Bの拒否を含む比較、T4の12枚と追加2枚、既定を維持する判断 |
| [保存・回収・分析の再レビュー](results/story-evidence-review.md) | 原本消失・配置喪失・部分的な分析と閲覧用更新、実データ保全とT4再検証 |
| [ストーリーの状態・保存更新・分析](results/story-evidence.md) | 終端判定、破損隔離、差分更新、要約と詳細、4B・T4、既存データ保全 |
| [物語計画・作品保存・公開API](results/story-workflow.md) | 自動枚数、比較の成否、作品移行、Python API、T4の接続・生成・終了 |
| [ストーリー生成](results/story-generation.md) | Fake・4B・T4による計画と順次保存、残る品質・起動の課題 |
| [品質ゲート・保存・評価](results/quality-storage-evaluations.md) | 累積検査、保存競合、AI向け評価記録とT4の生成・終了 |
| [準備・保存機能](results/seamless-baseline.md) | 保存構成の再編前の実機結果 |
| [統合時の保存保護](results/merge-integration.md) | 使用中の仮想環境・キャッシュ・一時ファイルの保護 |
| [保存・セット・操作の再編](results/repository-rebuild.md) | 今回の変更の検証範囲と環境上の制約 |

最新成功ポインターは全工程の成功後だけ更新します。全体が失敗した実行の部分成功は、成功ポインターへ昇格させません。CIとローカルの結果は、同じコミット・コードハッシュであることを確認して参照します。

2026年10月26日以降の明示的な容量整理で、過去の個別画像・作品・評価・ログ・検証workspaceを削除する。各記録は実施当時の結果を表し、現在も個別データが残っていることを意味しない。削除後は実画像による再確認ができない。対象と実際の削除結果は[容量整理の記録](results/klein-readiness.md)で確認する。
