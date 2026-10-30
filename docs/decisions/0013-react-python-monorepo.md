# ReactとPythonの配置・保存・検証

状態: 採用。日付: 2026-10-31。

## 背景

会話画面を同じPCで利用し、PythonライブラリとCLIからも同じ機能を呼ぶ。製品コードを一つのルートへ集約し、画面の修正でPython全体の検証を繰り返さない構成が必要になった。

## 決定

製品をsrc/frontend、src/backend、src/contractsへ分ける。backendは一つのPythonパッケージを維持する。テストは既存の分類と共有fixtureを維持するため、Pythonはルートのtests、画面はfrontend/testsへ置く。空の機能層は追加せず、既存の画像・終了処理はdomainとapplicationの契約を再利用する。

ReactはVite、React Router、TanStack Query、Tailwind CSS、Base UIを使う。公開Python APIをFastAPIで提供し、OpenAPIからTypeScriptクライアントを生成する。ビルドした画面をwheelへ同梱し、利用者にNode.jsを要求しない。

構造化記録はSQLite、実体はファイルに保存する。会話の本文と状態は一つのトランザクションで確定し、画像の送信と回収は独立した状態を持つ。操作ID、入力ハッシュ、所有プロセスを保存し、不明な外部要求を自動再送しない。旧記録は読み取り専用のarchiveへ隔離し、自動移行と旧生成入口を廃止する。

Nxはfrontend、backend、contracts、tools、e2eを選択する。Python内の関連テストは標準ライブラリのASTによる逆import依存で選ぶ。既存テストを機能別の別パッケージへ分割しないための限定的な選択処理で、製品コードをimportしない。共通設定と製品資源の変更は全Pythonテストへ広げ、PRと公開前は対象backendの全テストを実行する。相対import、定数による動的import、パッケージ初期化、入れ子のfixtureを対象に含める。解釈できない動的import、静的な検証素材、構文が読めない変更は全Pythonテストへ広げる。

キャッシュにはOS、Node、Python、ソース、ロック、タスク設定、依存タスクの配布物を含める。Pythonのテストは異なる選択範囲の結果や蓄積した検証記録を復元しないよう、タスクキャッシュを無効にする。部分検証と全体カバレッジを分け、公開前は配布物と隔離導入を確認する。実LLMとGPUは明示実行に限定する。

## 影響

HTTPとCLIは同じ保存・処理権を使う。新しいGPUの確保はstartだけで行い、workerへ会話、DB、LLM設定を配布しない。利用方法は[画面の手順](../guides/web.md)、現在の境界は[設計](../current/web.md)、検証の対象は[開発手順](../development/workflow.md)を参照する。
