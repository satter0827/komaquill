# Komaquill

ComfyUIで画像を生成し、画像と生成条件をローカルへ保存するPython CLIです。Google ColabのGPU、または起動済みのComfyUIを利用します。SD 1.5 FP16で512×512の画像を生成します。

## 必要な環境

- Windows x64またはARM64、PowerShell 7、Git、OpenSSHクライアント。
- GPUを利用できるGoogle Colabアカウント。利用可能なGPUはプラン・利用枠・空き状況によって変わります。
- VS Codeは任意です。

Pythonとuvは導入スクリプトがプロジェクト内へ配置します。Windows ARM64でもPythonはx64版を使用します。

## 環境を準備する

PowerShell 7でリポジトリのフォルダーを開き、1行ずつ実行します。

```powershell
./bootstrap/setup.ps1
. ./bootstrap/activate.ps1
komaquill --version
```

バージョンが表示されれば準備完了です。VS Codeの拡張機能も導入する場合は、最初のコマンドに `-InstallExtensions` を付けます。

## 起動して生成する

```powershell
komaquill start
```

初回は公式Colab CLIの案内に従ってGoogle認証を行います。認証コードは端末だけに入力します。
モデルを検証し、ColabのT4上でComfyUIを起動します。「接続しました」と表示されたら、その端末を開いたままにします。

別のPowerShellで同じフォルダーを開き、次を実行します。

```powershell
. ./bootstrap/activate.ps1
komaquill doctor
komaquill generate --prompt "a small cabin in a sunny forest, illustration"
```

画像、seed、プロンプト、ワークフローを `.artifacts/runs/実行ID/` へ保存します。保存先が表示されたら画像を確認できます。

## ComfyUIの画面を開く

```powershell
komaquill open --view app
komaquill open --view graph
```

`app` は入力欄と結果を中心に使う画面、`graph` はノードを編集する画面です。画面を開くと端末へ戻ります。画面から生成した画像も `start` が自動保存します。

## 終了する

```powershell
komaquill status
komaquill stop
```

生成と回収を待ち、所有するColabを終了します。`start` の端末でCtrl+Cを押しても同じ動作です。
生成完了を待たずに終了する場合は `stop --force`、または2回目のCtrl+Cを使います。回収できなかった要求は履歴へ残します。

## 起動済みのComfyUIを使う

```powershell
komaquill start --backend external --endpoint http://127.0.0.1:8188
```

同じ生成・保存コマンドを使用できます。Basic認証が必要な場合は `--username 利用者名` を指定し、パスワードを非表示の入力欄へ入力します。終了時に外部ComfyUIを停止することはありません。

## 保存場所

| フォルダー | 内容 |
|---|---|
| `.artifacts/runs/` | 画像、生成条件、生成履歴 |
| `.artifacts/models/` | モデル |
| `.artifacts/runtime/` | 実行環境、接続の管理記録、SSH鍵 |
| `.artifacts/logs/` | 実行ログ、環境一覧、診断情報 |
| `.artifacts/cache/` | 再作成可能なキャッシュ |

`.artifacts/`全体をGitの対象外にしています。モデル・画像・履歴を残す場合は、このフォルダーを削除しないでください。
Google認証と公式Colab CLIの管理ファイルは標準のユーザーフォルダー、外部接続のパスワードはOSの資格情報ストアへ保存します。

Google Driveは使いません。Colabは配布元からモデルを直接取得し、ランタイム内で再利用します。ローカルからモデルを毎回アップロードする必要はありません。

## 開発と詳しい使い方

- [開発への参加](CONTRIBUTING.md)
- [操作と障害対応](docs/usage.md)
- [構成と設定](docs/architecture.md)
- [開発とリリース](docs/development.md)
- [検証手順](docs/validation.md)

VS Codeの起動設定も同じCLIを呼び出します。プロンプトや認証コードは統合ターミナルへ入力します。
