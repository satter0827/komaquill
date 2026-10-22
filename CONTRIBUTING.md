# 開発への参加

## 環境を構築する

[必要な環境](README.md#必要な環境)を用意し、PowerShell 7でリポジトリのフォルダーを開きます。

```powershell
./scripts/setup.ps1 -InstallExtensions
. ./scripts/activate.ps1
python scripts/check.py
python scripts/security.py
```

すべて終了コード0で完了すれば、開発環境と品質検査を実行できています。検査にGPUやGoogle認証は必要ありません。

## 変更する

最新のmainから `feat/内容`、`fix/内容`、`docs/内容` などの作業ブランチを作成します。
CLIを操作の中心に置き、VS Codeには起動設定だけを置きます。生成・保存処理からColab制御への依存を追加しないでください。

根本原因と同じ原因がある箇所を確認し、目的に必要な範囲を変更します。標準ライブラリと成熟したライブラリを優先し、不要な処理は削除します。
PythonのdocstringはGoogle形式を基本とします。文書と利用者向けメッセージは日本語で記載します。

保存先は `.artifacts/` です。画像、モデル、ログ、認証情報をコミットしないでください。認証情報を引数やURLへ含めることも禁止します。

## 検証してPRを作成する

変更に対応する回帰テストを追加し、品質検査と秘密情報検査を実行します。配布対象を変更した場合は配布物も確認します。

```powershell
uv build --out-dir .artifacts/dist
python scripts/check_package.py
```

コミットは `fix: 回収失敗後も正常な画像を保存する` のように、変更の目的を日本語で記載します。
PRには解決する問題、変更後の動作、実施した検証、残る制約を記載します。
mainへのマージにはPRと必須CIの成功が必要です。マージはリポジトリ所有者が行います。

Colabへ転送するソースはHEADコミットです。変更したコードで実機検証する前にコミットしてください。GitHubへのpushは起動条件ではありません。

依存関係、検証、バージョン更新の手順は[開発とリリース](docs/development.md)を参照してください。
