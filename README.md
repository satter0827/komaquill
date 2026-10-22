# Komaquill

ローカルからGoogle Colab上のComfyUIを操作して画像を生成するPython CLIです。

画像、生成履歴、モデル、実行環境は `artifacts/` に保存し、Gitでは管理しません。

## 初めて使う

WindowsのPowerShell 7で、リポジトリのフォルダーを開いて実行します。

```powershell
./scripts/setup.ps1 -InstallExtensions
. ./scripts/activate.ps1
komaquill start
```

モデル約2.13GBをローカルへ取得し、Colabを開きます。ColabでGPUを選択し、鍵アイコンから
`KOMAQUILL_PASSWORD` を20文字以上のランダムな文字列で登録します。
表示されたコミットIDをノートブックへ入力し、セルを実行してください。
接続先URLが表示されたら、ローカルの入力にURLとパスワードを指定します。
パスワードはローカルの資格情報ストアに保存されます。

```powershell
komaquill doctor
komaquill generate --prompt "a small cabin in a sunny forest, illustration"
```

保存先は `artifacts/runs/実行ID/` です。画像に加え、実際のプロンプト、seed、ワークフロー、モデルと環境の識別情報が残ります。

## 画面を使う

```powershell
komaquill open --view app
komaquill open --view graph
```

App Modeは入力欄と結果を中心に使う画面、Node Graphはノードと接続を編集する画面です。
いずれも同じComfyUIを使い、CLIを起動している間に完了した画像を自動保存します。
ブラウザー側でもComfyUIのユーザー名 `komaquill` とパスワードを入力してください。
自動保存の終了はCtrl+Cです。未回収の結果は `komaquill collect` で回収できます。

**利用後は回収状況を確認し、Colabのランタイムを終了してください。** CLIの終了だけではGPUの利用は終了しません。

## VS Codeから使う

「実行とデバッグ」から「画像を生成する」「入力画面を開く」などを選びます。
同じCLIを呼び出すため、端末と生成結果・保存場所は共通です。プロンプトは統合ターミナルで入力します。
必要なときはPythonコードにブレークポイントを置けます。

## 資源の配置

- ローカル：作品と履歴、モデルの正本、実行環境、キャッシュ。
- Colab：GPU推論用の一時コピー。Google Driveのマウントは使用しません。
- GitHub：ソース、配布するワークフローJSON、設定例、文書。

モデルはローカルとColabが配布元から直接取得します。新しいランタイムでは再取得が必要です。
初期モデルはSD 1.5 FP16、既定の画像は512×512です。ストーリー分割や複数モデルへの対応は後続です。

## 文書

- [使い方と障害対応](docs/usage.md)
- [構成と設定](docs/architecture.md)
- [開発とリリース](docs/development.md)
- [検証結果](docs/validation.md)
