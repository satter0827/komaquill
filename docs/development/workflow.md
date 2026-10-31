# 開発とリリース

## 環境を準備する

[CONTRIBUTING](../../CONTRIBUTING.md#環境を構築する)に従ってPython環境を導入し、ルートで`pnpm install`を実行します。Pythonの依存はbackend、画面の依存はfrontend、検証とJavaScriptのlockfileはルートで管理します。

## 変更に応じて検証する

| コマンド | 対象 |
|---|---|
| `pnpm check` | 基準版との共通祖先以降の変更を扱う。Pythonはimport依存から関連テストを選択 |
| `pnpm check:ci` | 基準版から影響を受けるプロジェクトの全テストとビルド |
| `pnpm check:all` | 全プロジェクト、カバレッジ、配布物、隔離導入の検証 |
| `pnpm build` | Reactをビルドし、画面を同梱するwheelとsdistを作成 |
| `pnpm test:web` | 隔離した画面E2Eと操作ごとの証跡取得 |
| `pnpm review:web` | 主観評価用の操作・証跡収集。評価の合格判定は行わない |

`node scripts/python.mjs scripts/check.py --scope daily --plan`で対象プロジェクト、`node scripts/python.mjs scripts/affected.py --plan`でPythonの選択理由とテストを確認できます。比較基準の既定値はnx.jsonのdefaultBaseが正本です。`NX_BASE`を指定するとNxとPythonで同じ値を使用します。コミット済み・ステージ済み・作業中・新規・削除の変更を含めます。Reactの変更からPythonの全テストを呼び出す依存は設定しません。

lint、型検査、テスト、ビルドをNxの別タスクとして定義します。入力と出力は各project.jsonを正本とします。API契約は`pnpm contracts`でOpenAPIとTypeScriptクライアントを生成します。

## 結果を確認する

検証全体の対象範囲、基準版、入力ハッシュ、時刻、終了結果、出力ログは`.artifacts/records/reports/checks/`、Pythonの選択とJUnitは`.artifacts/records/reports/affected/`へ保存します。Nxのタスクキャッシュは`.artifacts/cache/nx/`です。キャッシュは静的検査、画面の単体テスト、契約、ビルドに使用します。画面E2Eとレビュー用収集は毎回新しい操作証跡を残し、Pythonの関連テストは実行範囲を毎回記録するためタスクキャッシュから復元しません。現在のGPU・プロバイダー観測にもキャッシュを使用しません。

局所検証の成功を全体成功として扱いません。中断、不足環境、入力変更があった実行は成功として記録しません。実LLMとGPUの検査は日常の検証から分離します。

カバレッジは製品・検査・環境更新のPythonを未実行ファイルも含めて計測し、testsを除外します。閾値は[pyproject.toml](../../pyproject.toml)を正本とし、行と分岐を独立して判定します。

## 配布と依存更新

`pnpm build`は`.artifacts/build/`で配布用ソースを組み立て、`.artifacts/dist/`へ出力します。Reactのビルド結果を追跡対象へ書き戻しません。通常の利用者にNode.jsを要求しません。

Python依存を更新する場合はbackendのuv.lockを更新し、`python -m bootstrap.update_worker_lock`でGPU転送用ロックを出力します。実機検査では日時、対象ハッシュ、環境、期待結果との差、停止確認を記録します。

mainへの変更はPRで行い、qualityとsecurityの成功を確認します。マージは所有者が行います。
