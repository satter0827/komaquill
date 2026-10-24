# 開発への参加

## 環境を構築する

[必要な環境](README.md#必要な環境)を用意し、PowerShell 7でリポジトリのフォルダーを開きます。

```powershell
./bootstrap/setup.ps1 -InstallExtensions
. ./bootstrap/activate.ps1
python scripts/check.py --level 2
```

終了コード0で完了することを確認します。品質検査にGPU、Google認証、モデルのダウンロードは必要ありません。LinuxではPython 3.12とuvを導入し、uv sync --locked --extra colab-clientで環境を準備します。CIはWindowsとLinuxで同じ検査を実行します。

## 変更する

最新mainからfeat/内容、fix/内容、docs/内容などの作業ブランチを作成します。[開発規約](AGENTS.md)に従い、設計・保存・秘密情報・文言・検証の規約を確認してください。scriptsは品質ゲート、bootstrapは導入処理、testsはテストを担当します。

[文書の索引](docs/README.md)から対象の要件ID・受入条件・現在設計を確認し、実装・テスト・操作手順を同じPRで更新します。未実装の計画と対象版の検証結果は別に管理します。

対応する不具合を再現するテストを追加し、同じ原因のある箇所も確認します。設定値と固定バージョンの管理方法は[構成と設定](docs/current/architecture.md)、依存更新と配布物の検証は[開発とリリース](docs/development/workflow.md)を参照してください。

## 検証する

```powershell
python scripts/check.py --level 2

```

終了コード0で完了することを確認します。整形確認、lint、型検査、構造検査、テスト、配布物、隔離wheel E2E、秘密情報検査を一つのコマンドで実行します。srcとscriptsのPythonを計測し、testsを除外して行90%以上・分岐80%以上を独立判定します。検査結果は.artifacts/records/reports/quality/実行ID/、配布物は.artifacts/dist/実行ID/へ保存します。wheel検証はリポジトリ外の一時環境を作成し、終了時に削除します。

GPU、SSH、起動・回収・終了を変更した場合は[実機検証](docs/verification/strategy.md)も実施します。作品・モデル・履歴・秘密情報は公開しないでください。

## PRとリリース

PRには問題、変更後の動作、検証結果、残る制約を記載します。関連する段階を複数PRへ分けた場合は先行PRを基点とし、そのマージ後にmainへ基点を揃えてCIを再確認します。必須のqualityとsecurityを通し、mainへのマージはリポジトリ所有者が行います。

バージョンと公開手順は[開発とリリース](docs/development/workflow.md#公開する)を参照してください。

公開前は python scripts/check.py --level 3 を実行します。レベルと結果の確認方法は[開発とリリース](docs/development/workflow.md)を参照してください。
