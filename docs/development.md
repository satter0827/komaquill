# 開発とリリース

## 品質確認

環境を準備して有効にしてから `python scripts/check.py` を実行します。
Ruffの整形確認・lint、strict mypy、Import Linter、pytestを実行します。
循環参照、CLIへの逆依存、本体からtests・scripts・開発専用ライブラリへの依存を禁止します。

テストはHTTP境界の模擬応答と一時フォルダーを使用し、有料API、GPU、モデルのダウンロードに依存しません。
結果と一時ファイルはartifacts/testsへ保存します。WindowsとLinuxのCIで同じ手順を実行します。

`python scripts/security.py` は追跡対象、ノートブック出力、作業ツリー、全Git履歴を検査します。
setup.ps1がpre-pushを登録するため、検査ツールがない状態では送信できません。
検査結果は伏せ字にしてartifacts/securityへ保存します。
秘密情報検査はdetect-secretsを使用し、検出した値を外部へ送って確認する機能を無効にします。
公開対象、ステージ済みの差分、全Git履歴を検査し、レポートには場所と種類だけを保存します。
公開リビジョン・SHA256と明示した試験値だけに、行単位の除外理由を付けています。

Windowsの実行制御に対応するため、CLIはPythonのモジュール実行に統一しています。
activate.ps1のkomaquill関数も `python -m komaquill` を呼び出します。
独自に生成された実行ファイルの許可設定を変更する必要はありません。

## バージョン

バージョンの正本はpyproject.tomlです。`uv version --bump patch` 等の標準機能でロックファイルと一緒に更新します。
番号はSemVerを基本とし、Pythonの事前版には `0.1.0rc1` 等のPEP 440表記を使用します。
0.xでは破壊的変更時にminorを上げ、変更点をPRとリリースノートへ記録します。

リリース時はmain上の版と一致する `v0.1.0` 等のタグを作成します。公開済みタグは変更・削除できません。
配布物は `uv build --out-dir artifacts/dist` で生成します。PyPIへの自動公開は行いません。

## Colab検証

実装したコミットをGitHubへ送信し、`komaquill start` から同じコミットのノートブックを使用します。
GPUで1枚生成し、doctor、両画面の起動、画面からの生成、自動回収、collectでの再回収を確認します。
初回準備時間、再生成時間、モデル・環境・ワークフローの識別情報を記録します。
Colab側の実際の依存バージョンはartifacts/runtime/environment.txtに保存します。
