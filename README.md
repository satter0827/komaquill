# Komaquill

ComfyUIで画像を生成し、画像と生成条件をローカルへ保存するPython CLIです。GPUにはGoogle Colabを使います。起動済みのComfyUIにも接続できます。

## 初めて使う

WindowsのPowerShell 7で、リポジトリのフォルダーを開きます。以下は1行ずつ実行してください。

```powershell
./scripts/setup.ps1 -InstallExtensions
. ./scripts/activate.ps1
komaquill start
```

初回は公式Colab CLIの案内に従ってGoogle認証を行います。認証コードは端末だけに入力してください。
モデルを確認し、T4上でComfyUIを起動します。「接続しました」と表示されたら、この端末を開いたままにします。

別のPowerShellで環境を有効にし、画像を生成します。

```powershell
. ./scripts/activate.ps1
komaquill doctor
komaquill generate --prompt "a small cabin in a sunny forest, illustration"
```

画像、プロンプト、seed、ワークフローと環境の識別情報を `artifacts/runs/実行ID/` へ保存します。

## 画面を使う

```powershell
komaquill open --view app
komaquill open --view graph
```

`app` は入力欄と結果を中心に使う画面、`graph` はノードを編集する画面です。画面を開くと端末へ戻ります。
画面から生成した画像も、`start` の端末が自動保存します。

## 終了する

```powershell
komaquill status
komaquill stop
```

生成と回収を待ち、所有するColabを終了します。`start` の端末でCtrl+Cを押しても同じ動作です。
生成完了を待たずに終了する場合は `stop --force`、または2回目のCtrl+Cを使います。
外部ComfyUIの場合はローカルの監視だけを終了します。

## 起動済みComfyUIを使う

```powershell
komaquill start --backend external --endpoint http://127.0.0.1:8188
```

同じ生成・保存コマンドを利用できます。Basic認証が必要な場合は `--username 利用者名` を指定し、パスワードを非表示の入力欄へ入力します。

## 保存場所

- `artifacts/runs/`：画像と生成履歴。
- `artifacts/models/`：モデルの正本。
- `artifacts/runtime/`：実行環境、接続の管理記録、SSH鍵。
- `artifacts/logs/`、`artifacts/cache/`：ログと再作成可能なキャッシュ。

`artifacts/` 全体をGitの対象外にしています。Google認証と公式Colab CLI自身の管理ファイルは、標準のユーザーフォルダーへ保存します。
Google Driveは使いません。Colabでは配布元からモデルを直接取得し、ランタイム内だけで再利用します。大容量モデルをWindowsから毎回アップロードしません。

初期モデルはSD 1.5 FP16、画像は512×512です。ストーリー分割と複数モデルへの対応は今後の対象です。

VS Codeの「実行とデバッグ」も同じCLIを呼びます。プロンプトは統合ターミナルへ入力します。

## 文書

- [使い方と障害対応](docs/usage.md)
- [構成と設定](docs/architecture.md)
- [開発とリリース](docs/development.md)
- [検証結果](docs/validation.md)
