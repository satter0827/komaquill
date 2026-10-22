# 開発とリリース

## 品質確認

環境を準備して有効にしてから `python scripts/check.py` を実行します。
Ruffの整形確認・lint、strict mypy、Import Linter、pytestを実行します。
循環参照、CLIへの逆依存、生成・保存からColab制御への依存、本体からtests・scripts・開発専用ライブラリへの依存を禁止します。

テストはHTTP境界の模擬応答と一時フォルダーを使用し、有料API、GPU、モデルのダウンロードに依存しません。
結果と一時ファイルはartifacts/testsへ保存します。WindowsとLinuxのCIで同じ手順を実行します。

`python scripts/security.py` は追跡対象、作業ツリー、全Git履歴を検査します。
setup.ps1がpre-pushを登録するため、検査ツールがない状態では送信できません。
検査結果は伏せ字にしてartifacts/securityへ保存します。
秘密情報検査はdetect-secretsを使用し、検出した値を外部へ送って確認する機能を無効にします。
公開対象、ステージ済みの差分、全Git履歴を検査し、レポートには場所と種類だけを保存します。
公開リビジョン・SHA256と明示した試験値だけに、行単位の除外理由を付けています。

Windowsの実行制御に対応するため、CLIはPythonのモジュール実行に統一しています。
activate.ps1のkomaquill関数も `python -m komaquill` を呼び出します。
独自に生成された実行ファイルの許可設定を変更する必要はありません。

Windows ARM64でも、公式Colab CLIの依存がWindows x64向けバイナリを配布しているため、Windows x64 Pythonを使用します。
PyArrowとcryptographyのARM64ソースビルドや依存の省略は行いません。uv本体はOSのアーキテクチャに合わせます。

## バージョン

バージョンの正本はpyproject.tomlです。`uv version --bump patch` 等の標準機能でロックファイルと一緒に更新します。
番号はSemVerを基本とし、Pythonの事前版には `0.1.0rc1` 等のPEP 440表記を使用します。
0.xでは破壊的変更時にminorを上げ、変更点をPRとリリースノートへ記録します。

リリース時はmain上の版と一致する `v0.1.0` 等のタグを作成します。公開済みタグは変更・削除できません。
配布物は `uv build --out-dir artifacts/dist` で生成します。PyPIへの自動公開は行いません。
作成後に `python scripts/check_package.py` で版の一致、必要なワークフローの同梱、生成物の混入がないことを確認します。

## Colab検証

公式CLIの更新時は、まずWindows上でCPUの作成、実行、転送、SSH経由のHTTPとWebSocket、終了確認を実施します。
この確認が成功してからT4で生成、両画面、自動回収、重複防止、再接続、通常・強制終了を検証します。
ソースはローカルでコミットすれば転送でき、事前のpushは不要です。実行ログとパッケージ一覧はartifacts/logsへ回収します。

外部ComfyUIは専用の識別ファイルなしで確認します。接続前の画像を取り込まず、終了時に外部サーバーへ停止要求を送らないことを確認します。
実機結果は [検証結果](validation.md) へ記録します。認証情報・接続トークン・私有の実行ログは公開しません。
