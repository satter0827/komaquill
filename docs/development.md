# 開発とリリース

## 品質検査

[環境構築](../CONTRIBUTING.md#環境を構築する)の後、次を実行します。

```powershell
python scripts/check.py
python scripts/security.py
```

check.pyはRuffの整形確認・lint、strict mypy、Import Linter、pytestを実行します。
srcとscriptsのPythonコードを計測し、行90%以上・分岐80%以上をそれぞれ判定します。testsは計測対象に含めません。
結果は`.artifacts/reports/quality/`へ保存します。
循環参照、CLIへの逆依存、生成・保存からColab制御への依存、本体から開発用モジュールへの依存を禁止します。
テストは模擬応答と一時フォルダーを使用し、GPU、Google認証、モデルのダウンロードに依存しません。

security.pyは公開対象、ステージ済み差分、全Git履歴をdetect-secretsで検査します。検出値を外部送信する確認機能は無効にしています。
レポートは場所と種類だけを`.artifacts/reports/quality/security.json`へ保存します。公開リビジョン、SHA256、明示した試験値の除外には行単位の理由を記載します。
setup.ps1は送信前の検査をGitフックへ登録します。WindowsとLinuxのCIも同じ検査を実行します。

## 実行環境と依存関係

CLIと開発ツールは同じPython環境のモジュールとして実行します。PowerShellのkomaquill関数は`python -m komaquill`を呼び出します。
Windows ARM64では公式CLIのバイナリ依存に合わせてx64 Pythonを使用します。uv本体はOSのアーキテクチャに合わせます。
Pythonの版は.python-versionで固定し、GPU側も同じ版の専用環境を使用します。

依存の変更はpyproject.tomlとuv.lockを同時に更新します。ローカルの公式CLI依存とGPU依存を分離し、ローカルへGPU用ライブラリを導入しないでください。
公式CLIやComfyUIの更新時は、[実機検証](validation.md)も実施します。

## 配布物

```powershell
uv build --out-dir .artifacts/dist
python scripts/check_package.py
```

sdistとwheelの版、同梱ワークフロー、生成物と資格情報の除外を確認します。ビルド結果は`.artifacts/dist/`へ保存します。

## バージョンとリリース

バージョンの正本はpyproject.tomlです。SemVerを基本とし、Pythonの事前版にはPEP 440形式を使います。
`uv version --bump patch`などでロックファイルと一緒に更新します。0.xの破壊的変更ではminorを上げます。

必須CIが成功したmain上で、宣言した版と一致する`v0.1.0`などのタグを作成します。公開済みタグは変更・削除できません。
リリースノートには利用者への変更点と必要な操作を記載します。PyPIへの自動公開は行いません。
