# 文書の索引

画面の装飾と共通操作の更新は[視覚設計と操作の検証](verification/results/cherry-ui.md)を参照してください。

短い画面・文字拡大と実LLMの再接続を要件から再確認した結果は[送信領域の再レビュー](verification/results/cherry-zero-base-review.md)を参照してください。

マージ後の検査で確認した状況パネルの開閉と画面幅の切替は[状況パネルの再配置](verification/results/state-panel-reflow.md)を参照してください。

会話・行動・アルバムの操作と監視の分担は[会話とアルバム](current/web.md#会話とアルバム)、利用手順は[会話画面](guides/web.md)、確認結果は[会話とアルバムのUI検証](verification/results/conversation-album-ui.md)を参照してください。

要件から再確認した受付確認と画像失敗時の復旧は[会話とアルバムの再レビュー](verification/results/zero-base-conversation-album-review.md)を参照してください。

受付応答を失った後の画面遷移と、監視の診断ログを再確認した結果は[描画の受付確認と画面遷移](verification/results/drawing-receipt-lifecycle-review.md)を参照してください。

発言と行動の同時送信、生成に必要な接続確認、本文保存と画像生成の分離、固定NSFWへの移行は[操作と接続確認](current/operations-and-availability.md)を参照してください。

確認範囲と未確認事項は[会話・描画・画面構成の検証](verification/results/conversation-ui-operations.md)に記録しています。

受付・接続確認・失敗後の表示を再検査した結果は[会話・描画・画面構成の再レビュー](verification/results/zero-base-ui-operations-review.md)を参照してください。

実4B・9BとT4で確認した不具合、保存・復旧の再検査、残る生成品質の制約は[実LLM・T4の再検証](verification/results/live-ui-operations.md)を参照してください。

## 読み始める場所

| 作業                                            | 最初に読む文書                                                          | 次に確認するもの                                                                                                                                                                                                                |
| ----------------------------------------------- | ----------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 利用を始める                                    | [README](../README.md)                                                  | [操作](guides/usage.md)                                                                                                                                                                                                         |
| 要件を変更する                                  | [目的と範囲](product/scope.md)                                          | [要件台帳](requirements/catalog.toml)                                                                                                                                                                                           |
| 会話とReact画面を使う                           | [会話画面](guides/web.md)                                               | [現在の設計](current/web.md)、[統合計画](plans/roleplay-chat.md)                                                                                                                                                                |
| コードを変更する                                | [開発規約](../AGENTS.md)                                                | [現在の構成](current/architecture.md)、[変更・検証](development/workflow.md)                                                                                                                                                    |
| Pythonから利用する                              | [Python API](guides/python-api.md)                                      | [現在の構成](current/architecture.md)                                                                                                                                                                                           |
| ストーリーから生成する                          | [ストーリーの生成](guides/stories.md)                                   | [計画・生成・保存の契約](current/stories.md)                                                                                                                                                                                    |
| 過去の生成を評価する                            | [評価の操作](guides/evaluations.md)                                     | [評価の契約](current/evaluations.md)                                                                                                                                                                                            |
| 生成条件を変える                                | [プリセット](current/presets.md)                                        | [モデル準備](guides/preparation.md)                                                                                                                                                                                             |
| 参照画像から編集する                            | [参照画像による編集](guides/story-workflows.md)                         | [段階生成の契約](current/story-workflows.md)                                                                                                                                                                                    |
| 作業場所と生成物を整理する                      | [保存と整理](operations/artifacts.md)                                   | [検証結果](verification/results/story-stability.md)                                                                                                                                                                             |
| 接続・保存を調査する                            | [障害対応](operations/diagnostics.md)                                   | [保存と整理](operations/artifacts.md)                                                                                                                                                                                           |
| 保存済み画像がプリセットに表示されない          | [画像参照の復旧](current/image-recovery.md)                             | [障害対応](operations/diagnostics.md)                                                                                                                                                                                           |
| 設計判断を確認する                              | [設計判断](decisions/0001-native-interfaces.md)                         | [契約](current/contracts.md)                                                                                                                                                                                                    |
| 受入検証を行う                                  | [検証戦略](verification/strategy.md)                                    | [検証記録](verification/README.md)                                                                                                                                                                                              |
| 画面を検査してAIへ証跡を渡す                    | [画面のE2Eとレビュー用証跡](verification/web-evidence.md)               | [変更・検証](development/workflow.md)                                                                                                                                                                                           |
| 今後の機能を検討する                            | [今後の候補](plans/future.md)                                           | [保存の設計判断](decisions/0002-preservation-and-cleanup.md)                                                                                                                                                                    |
| 個人設定・プリセットを使う                      | [会話画面](guides/web.md)                                               | [設定と作成の契約](current/creators.md)、[再レビューの記録](verification/results/zero-base-play-review.md)、[会話と画像の改善項目](plans/creators-and-play.md)                                                                  |
| LLMの実行基盤と会話品質を改善する               | [LLM処理・文脈管理・画像反映の改善計画](plans/langgraph-and-context.md) | [現在の設計](current/creators.md)、[設計判断](decisions/0014-conversation-state-and-memory.md)、[検証結果](verification/results/conversation-state-and-images.md)、[再レビュー](verification/results/zero-base-state-review.md) |
| 設定・API取得・文書解析・画面検査の重複を減らす | [標準機能への委譲計画](plans/library-adoption.md)                       | [採用判断と制約](decisions/0015-library-delegation.md)、[要件台帳](requirements/catalog.toml)、[検証戦略](verification/strategy.md)                                                                                             |

[会話実行とコンテンツ設定](current/execution-and-content.md)は、公式チェックポイント、履歴の分離、NSFW、固定メモ、場面の分岐と共有用の出力を扱います。設計判断は[会話実行の永続化](decisions/0017-durable-conversations-and-content.md)を参照してください。

文脈予算と原文参照による記憶、事実回答と画像の不採用判断は[原文を根拠にした会話](decisions/0018-source-backed-conversation.md)、実機結果は[会話とWAIの受入検証](verification/results/llm-acceptance.md)を参照してください。

## 文書の状態と正本

設定・LLM・API取得・検査の現在の分担は[標準機能への委譲](current/library-delegation.md)を参照してください。

`current/`は実装済みの構成と契約、`plans/`は未完了の計画を扱います。要件台帳のstatusは実装状態であり、実機で確認済みという意味ではありません。検証の対象版・日時・環境は`verification/`で別に確認します。

実装範囲と残る受入条件は[会話ライブラリ・APIサーバー・React画面の統合計画](plans/roleplay-chat.md)で管理します。製品コードをsrcへまとめ、その中でReact、Python、API契約を分け、画面から会話と画像の基本操作を完成させる計画です。既存ブラウザと単発画像生成も維持します。旧Storyの新規生成入口は廃止し、既存記録はarchiveへ分離しました。最新の確認結果と残存課題は[ReactとAPIの再レビュー](verification/results/react-review.md)を参照してください。

設定値・固定バージョン・エラーコードの正本はコードと設定です。文書には責務、使い方、制約、参照先を記載します。変更するときは要件ID、受入条件、実装、テスト、操作・運用手順を同じPRで対応付けます。

AIによる変更でも、この索引とAGENTSから対象文書を選び、実装状態と検証対象版を確認します。計画だけを根拠に実装済みと判断しません。ファイル・節へのリンクと要件の参照先は品質ゲートで確認します。

Kleinのモデル構成、役割の構成図、旧構成の削除理由は[設計判断](decisions/0009-klein-completion.md)、完走の条件は[短編漫画の検証](verification/story-completion.md)を参照してください。

複数題材での検証と、物語の状態・描画対象の分離は[設計判断](decisions/0010-multi-story-verification.md)を参照してください。

Klein 4B・9Bの切替、負プロンプトの廃止、GPUとCUの制限は[設計判断](decisions/0011-klein-model-comparison.md)、比較の操作と判定は[モデル比較](verification/klein-comparison.md)を参照してください。

モデルと操作の共通化、AniEdit・WAI構成、直接取得の判断は[設計判断](decisions/0012-model-interface-and-acquisition.md)、実施中の確認事項は[移行計画](plans/model-interface.md)を参照してください。

複数題材の未達解消は[継続計画](plans/story-stability.md)、実行と判定は[継続検証](verification/story-stability.md)を参照してください。

右ペインからの行動と、会話・画像へ反映する状態の契約は[自分の行動と現在の場面](current/player-actions.md)、判断の背景は[行動の確定と視覚情報の共有](decisions/0016-player-actions-and-visual-state.md)を参照してください。

今回の機能確認、実4B・WAIの結果と残る制約は[自分の行動と現在場面の検証](verification/results/player-actions-and-scenes.md)を参照してください。

main反映後の機能再レビューと保存復旧の修正は[行動・外見確認・保存復旧の再レビュー](verification/results/zero-base-player-review.md)を参照してください。
