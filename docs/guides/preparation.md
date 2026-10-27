# モデルを準備する

## 起動前の確認

Hugging Faceは標準CLIの認証を利用します。配布元が要求する利用条件への同意は利用者が行います。Civitaiで認証が必要な場合は、APIキーをOS資格情報ストアのサービス`komaquill-models`、ユーザー名`civitai`へ登録します。キーを設定ファイル、引数、URL、チャットへ貼り付けないでください。

```powershell
hf auth login
python -m keyring set komaquill-models civitai
```

公開取得できる配布元ではAPIキーは不要です。Detail Tweakerは作者のHugging Face配布を使用します。取得先、固定版、サイズ、SHA256、利用条件は[モデル定義](../../src/komaquill/resources/models/)で確認します。アクセス確認では少量のGETを使い、HEADだけでは権限不足と判断しません。PCへモデル本体を保存しません。

## 起動と追加準備

```powershell
komaquill start --model klein-4b --model wai-illustrious
komaquill open --model wai-illustrious --operation generate --view app
```

有効モデルに必要な資源だけをColabが取得します。順序は環境準備、モデル取得・SHA256照合、ComfyUI起動です。新しいGPUでは再取得が必要です。同じGPUでは照合済みファイルと中断した部分ファイルを再利用できます。

標準画面で追加ワークフローを開いた場合はPrepare Workflowを使います。モデル名、サイズ、取得先を確認して確定します。CLIの`prepare`も同じ確認と取得を使い、GPUを新規確保しません。所有を照合できない外部接続へ認証付き準備を自動適用しません。

ブラウザーは確認済みジョブを作り、接続中の監視が同じジョブIDを実行します。GPU側で資源単位の排他、部分取得、取消、SHA256検査を行います。取得待機で画像回収、CU照会、停止を妨げません。準備プロトコルが異なる旧サーバーは再起動してください。

## 中断と容量

取得失敗では初発の原因とジョブIDを確認します。通信断を理由にPC保存へ切り替えません。残容量と期限を確認し、準備を継続できなければ通常の回収・終了へ進みます。GPU停止を確認できない場合は所有記録を残します。

PCに残る旧モデルを整理する場合は、稼働停止、所有解放、対象一覧・サイズ・SHA256の保存後に対象ファイルを限定して削除します。作品、履歴、認証、実行環境、計画用ローカルLLMは別に保持します。
