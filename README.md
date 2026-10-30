# Komaquill

ストーリーから本文付きの画像を順次生成し、作品と生成条件をローカルへ保存するPython CLIです。単画像の生成にも対応します。Google ColabのGPU、または起動済みのComfyUIを利用します。標準構成はFLUX.2 Klein 4Bと指定のテキストエンコーダー、二つのLoRAです。生成と参照画像編集を同じ構成で行います。

## 必要な環境

Windows x64またはARM64、PowerShell 7、OpenSSHクライアントを使用します。ソースから導入する場合はGitも必要です。Colabを使う場合はGPUを利用できるGoogleアカウントを用意してください。VS Codeは任意です。

## ソースから導入する

PowerShell 7でリポジトリのフォルダーを開き、1行ずつ実行します。

```powershell
./bootstrap/setup.ps1
. ./bootstrap/activate.ps1
komaquill --version
```

バージョンが表示されれば導入完了です。Pythonとuvは.artifacts/runtime/へ配置します。Windows ARM64ではx64 Pythonを使用します。VS Codeの拡張機能も導入する場合はsetup.ps1へ-InstallExtensionsを指定します。

## wheelから導入する

uvを用意し、配布されたwheelを専用環境へ導入します。ファイル名のパスとバージョンは配布物に合わせてください。

```powershell
New-Item -ItemType Directory ./my-images
cd ./my-images
$env:UV_PYTHON_INSTALL_DIR = "$PWD/.artifacts/runtime/python"
$env:UV_CACHE_DIR = "$PWD/.artifacts/cache/uv"
uv venv --python 3.12 .artifacts/runtime/venv
uv pip install --python .artifacts/runtime/venv/Scripts/python.exe '../komaquill-0.1.0.dev0-py3-none-any.whl[colab-client,llm]'
. ./.artifacts/runtime/venv/Scripts/Activate.ps1
komaquill init
komaquill --version
```

外部ComfyUIだけを使う場合、[colab-client]は不要です。initは設定ファイルと保存先を準備します。既存の設定は上書きしません。Gitリポジトリは必要ありません。別の端末では作業フォルダーを開き、同じActivate.ps1で環境を有効にします。

## 起動して画像を生成する

```powershell
komaquill start
```

初回のGoogle認証は公式Colab CLIの案内に従います。認証コードはその端末だけに入力してください。モデルを検証し、ColabのT4上でComfyUIを起動します。接続後は端末へ戻ります。画面を使う場合はopenを実行します。接続と自動回収はバックグラウンドで続くため、端末やVS Codeを閉じられます。終了する場合は`komaquill stop`を実行します。起動後に画面も開く場合だけ`start --open`を指定します。

同じ作業フォルダーで次を実行します。ソースから導入した場合は、その端末でもbootstrap/activate.ps1を読み込みます。

```powershell
komaquill doctor
komaquill generate --prompt "a small cabin in a sunny forest, illustration"
```

候補数は`--candidates`で指定します。プロンプトをファイルから渡す場合は`--prompt-file positive.txt`、対象別の説明も含める場合は`--input-file image-input.json`を使います。入力形式は[操作](docs/guides/usage.md)を参照してください。保存先が表示されたら、.artifacts/data/runs/実行ID/の画像を確認します。プロンプト、seed、ワークフロー、画像の検証情報も同じフォルダーへ保存します。

## 会話画面を使う

ソースから開発する場合は、Python環境の導入後にルートで`pnpm install`と`pnpm dev`を実行し、表示されたローカルURLを開きます。

会話の作成、反復入力、状態の確認、保存と復元、単発画像生成、実行環境の管理を行えます。`fake`は`Hello`に対する固定サンプルです。任意の文章は実モデルのプロファイルを選び、送信ごとに実行を許可します。

配布版は`web`追加依存を導入し、`komaquill --project 作業フォルダー web --open`で起動します。Reactはwheelに同梱し、利用者の環境にNode.jsを要求しません。詳しくは[会話画面の操作](docs/guides/web.md)を参照してください。

## ComfyUIの画面を使う

```powershell
komaquill open --view app
komaquill open --view graph
```

appは画像説明と結果を表示するApp Mode、graphは標準ノードを編集するNode Graphです。どちらも同じ接続とGPUを使い、openではGPUを追加確保しません。画面からの生成もstartが実際に実行されたグラフとともに自動保存します。標準の画像・マスクのアップロードとSaveしたワークフローも保全します。未保存の編集内容は対象外です。保存と復元の範囲は[素材の保全](docs/guides/preservation.md)を参照してください。openはブラウザーを起動したら端末へ戻ります。

VS CodeではRun and DebugからStart Sessionを起動し、接続後にOpen GeneratorやGenerate Imageを選びます。Open GeneratorはApp Modeが既定です。環境導入はSetup Environment、品質検査はCheck Project、配布物の作成はBuild Packageから実行できます。Export Storyは要約または完全な記録をJSONへ出力します。どの入口も同じCLIと品質ゲートを呼び出します。

## プリセットを選ぶ

標準の生成セットは`klein-4b`、参照画像を使う編集は`klein-4b-edit`です。同じモデルの生成と編集は必要資源を共有します。WAI Illustriousもモデルとして選択できます。生成条件とモデルの固定版は[プリセット](docs/current/presets.md)を参照してください。WAIは負プロンプトに対応します。未指定は既定値、明示指定は置換、空文字は無効化です。Kleinへの明示指定は拒否します。

App Modeではプロンプト、候補数、編集用参照画像を操作します。LoRA強度などの詳細はNode Graphで調整できます。

```powershell
komaquill start
komaquill open --view app --model klein-4b
komaquill open --view graph --model klein-4b --operation edit
komaquill generate --model klein-4b --prompt "a sunny forest"
```

公開テンプレートの不足モデルは、画面のPrepare Workflowまたは`komaquill prepare ./workflow.json`で準備できます。モデル名・容量・取得元を確認してから取得します。詳細は[ワークフローの準備](docs/guides/preparation.md)を参照してください。設定やワークフローの追加方法、資源の利用条件は[プリセットの管理](docs/current/presets.md)を参照してください。

## 状態確認と終了

```powershell
komaquill status
komaquill collect
komaquill stop
```

stopは生成と回収を待ち、所有するColabを終了します。起動待ち中と`start --foreground`のCtrl+Cも同じ動作です。生成を待たずに終了する場合はstop --force、または2回目のCtrl+Cを使います。未回収要求は回収不能として件数と履歴を残します。電源断によるGPU終了は保証できないため、次回のstatusとstopで確認してください。

## 起動済みのComfyUIへ接続する

```powershell
komaquill start --backend external --endpoint http://127.0.0.1:8188
```

同じ生成・保存コマンドを使えます。Basic認証が必要な場合は--usernameを指定し、パスワードを非表示の入力欄へ入力します。外部接続の終了は監視だけを停止します。外部サーバーは停止しません。

## 保存場所

| フォルダー | 内容 |
|---|---|
| .artifacts/data/stories/ | 作品一覧、原文、計画、本文と画像、生成試行、LLMの可視出力 |
| .artifacts/data/runs/ | 単画像の画像、生成条件、履歴 |
| .artifacts/data/inputs/ | 入力画像・マスクの版と参照情報 |
| .artifacts/data/workflows/ | 保存したワークフローと入力の参照情報 |
| .artifacts/data/models/ | 廃止したモデル保存領域（整理前の原本） |
| .artifacts/state/ | 所有記録、接続情報、SSH鍵 |
| .artifacts/records/logs/ | 実行ログ、状態変化、終了結果 |
| .artifacts/runtime/ | 再構築できる実行環境と転送物 |
| .artifacts/records/reports/ | 検査結果 |
| .artifacts/cache/ | 再作成可能なキャッシュ |
| .artifacts/tmp/ | 一時ファイル |
| .artifacts/dist/ | wheelとsdist |

.artifacts/全体をGitの対象外にしています。作品を残す場合はこのフォルダーを削除しないでください。Google認証と公式Colab CLIの管理ファイルは標準のユーザーフォルダー、外部接続のパスワードはOSの資格情報ストアへ保存します。

Google Driveは使いません。PCは配布情報とアクセス権を確認し、モデル本体はColabが直接取得してSHA256を検証します。Colab内のモデルはランタイム内で再利用し、毎回のアップロードは行いません。新しいランタイムでは取得が必要です。

## 詳細

- [操作と障害対応](docs/guides/usage.md)
- [構成と設定](docs/current/architecture.md)
- [開発への参加](CONTRIBUTING.md)
- [開発とリリース](docs/development/workflow.md)
- [検証手順](docs/verification/strategy.md)

## 生成物を整理する

セッションの停止後、`komaquill clean --dry-run`で候補を確認し、`komaquill clean`で整理します。作品・資源はdata、ログ・検証記録はrecordsとして保持します。所有・復旧に必要なstateと実行環境runtimeも削除しません。[削除範囲](docs/operations/artifacts.md)と[バックアップ](docs/operations/backup.md)を確認してください。

## 設計と開発資料

[文書の索引](docs/README.md)から、目的・要件、実装済み設計、操作・開発・運用、検証結果、今後の計画を参照できます。CLIと同じ処理をPythonから利用する方法は[公開API](docs/guides/python-api.md)を参照してください。

生成履歴をAIで比較・分析する出力と評価の登録は[評価の操作](docs/guides/evaluations.md)を参照してください。自動評価や外部送信は行いません。
