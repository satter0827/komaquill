# 操作と障害対応

## 起動と接続

[導入手順](../../README.md#ソースから導入する)で環境を有効にし、`komaquill start` を実行します。初回のGoogle認証は公式Colab CLIの案内に従います。

設定はstart時に確定します。起動中に設定ファイルを変更しても反映されません。stop後のstartで新しい設定を使用します。

`start`は監視プロセスをバックグラウンドで起動します。準備が完了すると端末へ戻ります。端末を閉じてもSSH接続と自動回収を維持します。同じ設定で再実行すると既存の監視を再利用します。設定が異なる場合は停止を案内します。`--open`を明示した場合だけ画面を開きます。
GPUを新しく確保する操作はstartだけです。status、doctor、collect、stopはGPUを作成しません。

GPUへは実行中のパッケージから転送物を作成します。コードや起動設定が残存ランタイムと異なる場合は、stop後に起動し直します。

## 画像を生成する

```powershell
komaquill generate --prompt "a small cabin" --seed 42
komaquill open --view app
komaquill open --view graph
```

プロンプトを省略すると入力を求めます。Kleinへの画像説明には、対象、動作、小物の状態を自然な文章で記述します。品質用の既定文は標準ノードで付加します。入力画面とノード画面はComfyUIの標準機能です。
外部ComfyUIでは、画面を開いた後に表示されたワークフローJSONを読み込みます。

## 結果を回収する

```powershell
komaquill collect
komaquill collect 実行ID
```

`.artifacts/data/runs/実行ID/`にrun.json、workflow-api.json、画像を保存します。画面用のworkflow.jsonは取得できる場合に保存します。
CLIの要求は送信前、画面の要求は監視で確認した時点で記録します。受付不明の要求は自動再送せず、キューと履歴を照合します。

1件の取得失敗があっても、ほかの正常な画像の保存を続けます。今回の回収失敗がある場合、collectは対象IDを表示して終了コード1を返します。
接続先が残っている間に再回収してください。保存済み画像は重複取得しません。
読めないrun.jsonがある場合は上書きせず、対象IDを報告して正常な要求の回収を続けます。対象ファイルとアクセス権を確認してから再実行してください。

外部接続では、接続開始前のキューと履歴を除外します。既に記録した未回収要求は照合を続けます。

## 状態を確認する

```powershell
komaquill status
komaquill doctor
```

statusはColab、自動回収、接続、未回収と回収不能の件数を表示します。doctorは生成に必要なGPU、モデル、標準ノードを確認します。

| 履歴の状態 | 意味 |
|---|---|
| prepared | 要求を準備した |
| unknown | 受付結果が未確認 |
| queued | 受付済み。生成または回収を待っている |
| saved | 画像と生成条件を保存した |
| failed | 生成要求またはComfyUIでの生成が失敗した |
| lost | Colabが終了し、画像を回収できない |

画像取得や保存だけの失敗はqueuedのまま再試行できます。回収不能の理由はrun.jsonのerrorへ保存します。

## 終了する

```powershell
komaquill stop
komaquill stop --force
```

通常終了は生成と回収を既定10分まで待ちます。強制終了は生成完了を待たず、回収可能な結果を保存します。ログ取得と終了確認には別途通信時間がかかります。回収やログ取得が失敗した場合は終了コード1を返します。GPUの終了結果は個別に表示するため、エラーがあってもGPUが終了している場合があります。
起動待ち中、または`start --foreground`の端末では、Ctrl+Cが通常終了、2回目のCtrl+Cが強制終了に対応します。バックグラウンドの監視は`stop`で終了します。

監視が停止した場合も、stopは所有するランタイムを確認して終了します。SSH接続だけが失われていれば、既存ランタイムへの接続を復旧します。
Colabの終了確認後、未回収要求はlostになります。外部接続の終了は監視だけを停止し、外部サーバーやキューは変更しません。

## 障害に対応する

1. statusでColab、自動回収、未回収の状態を確認します。
2. 接続可能ならcollectで結果を回収します。
3. `.artifacts/records/logs/ランタイムID/`の準備ログ・実行ログ・capture.jsonを確認します。shutdown.jsonで画像回収・ログ取得・GPU終了の結果を確認します。
4. 起動または終了に失敗した場合はstopを再実行し、所有するColabの終了を確認します。

操作の経緯はevents.jsonl、ログの取得日時と最終成功日時はcapture.jsonへ保存します。
終了処理の診断には発生時刻・操作・ランタイムID・例外種別を保存します。例外本文や応答本文は保存しません。
SSHの復旧や接続情報の読み取りに失敗しても、所有するColabの終了を試みます。
終了を確認できない場合や履歴の確定に失敗した場合は管理記録を保持します。管理記録を手動削除すると所有するGPUを追跡できなくなるため、先にColabの利用状態を確認してください。
PCの電源断ではGPU終了を保証できません。次回のstatusとstopで確認します。観測前に失われた画面の要求や、消失したランタイムの画像は復元できません。

接続ポートの競合は起動済みのstartと設定を確認します。ホスト鍵が変わった接続は拒否します。Google認証とGPUの利用枠はColab側で確認してください。
別フォルダーから使う場合は`komaquill --project 作業フォルダー コマンド`を指定します。

## ログを読む

`komaquill logs --follow`で状態変化と失敗を表示します。表示のCtrl+Cは監視を停止しません。調査時は`komaquill start --foreground`で同じ監視を端末で実行できます。


| ファイル | 内容 |
|---|---|
| logs/monitor.log | バックグラウンド監視の標準出力とエラー |
| logs/cli.jsonl | ランタイム確保前を含むCLIの状態変化と失敗 |
| logs/ランタイムID/events.jsonl | 接続、要求状態、回収・終了の状態変化と失敗 |
| logs/ランタイムID/bootstrap.log | GPU側の導入と起動 |
| logs/ランタイムID/comfy.log | ComfyUIの実行出力 |
| logs/ランタイムID/environment.txt | GPU側の導入済み依存 |
| logs/ランタイムID/capture.json | ファイルごとの取得日時、成否、最終成功日時 |
| logs/ランタイムID/shutdown.json | 回収、ログ、GPU、接続、履歴確定それぞれの結果 |

パスは.artifacts/を基準にしています。取得に失敗したファイルは以前の内容を保持するため、ログ本文だけでなくcapture.jsonの最終成功日時も確認してください。保存済みの過去のログにcapture.jsonがない場合は取得日時が不明です。

診断のfailureはcode、stage、exception_type、http_status、retryableを持ちます。通信、画像検証、保存を区別し、retryableがtrueなら接続と保存先を確認してcollectを再実行します。run.jsonには画像のSHA256、サイズ、寸法、観測・更新・保存時刻を記録します。generation_started_atとgeneration_completed_atはComfyUIが報告した時刻だけを使用し、不明な値はnullです。

## VS Codeから実行する

Setup Environmentで導入し、Python: Select Interpreterで.artifacts/runtime/venvを選びます。Run and DebugのStart SessionがCLIのstartを呼び出します。Open GeneratorはApp ModeまたはNode Graphを選択でき、Generate Imageは正のプロンプトを入力し、負の条件と候補数は設定の既定値を使います。Generate Image from Fileは正負のプロンプトと対象別説明を記載したJSONのパスを入力します。

Prepare WorkflowはJSONのパス、Stop Sessionは通常・強制、Clean Generated Filesはプレビュー・整理を選びます。整理の既定はプレビューです。Show Status、Check Connection、Collect Results、Show Logsも同じCLIを使います。Run CLI with Argumentsでは任意の引数、Debug Sessionでは前面監視を実行します。画面だけを閉じてもGPUは終了しないため、Stop Sessionを実行してください。

品質検査はCheck Project、配布物作成はBuild Packageを使います。これらはCLIの別実装を持たず、選択したPythonと既存スクリプトを呼び出します。

## グラフを編集する

`open --view graph`は標準のNode Graphを開きます。App Modeと別のGPUを確保しません。ノードの追加・接続、サンプラー、モデル、LoRAなどを編集できます。標準ノード以外を使う場合は、そのノードと対応資源をGPU側へ導入する必要があります。

実行したグラフは生成条件として保存します。既定プリセットの値で上書きしません。残したい編集内容はComfyUIのSaveで保存してください。標準のSaveとアップロードは[素材保全](preservation.md)の対象です。Colabの一時領域だけに置いたファイルはランタイム終了時に失われます。

## GPUの所有を確認する

Googleの安定したアカウント識別子から作ったSHA256と、確保したエンドポイントIDを所有記録へ保存します。メールアドレスやアクセストークンは保存しません。終了は同じアカウントでプロバイダーの一覧を取得し、対象の不在を確認してから確定します。

公式CLIのローカル記録がなくても、対象が動作していれば既存GPUへ接続して回収・終了を試みます。認証失敗、通信失敗、アカウント不一致は不明として扱い、所有記録を保持します。認証と接続を確認して`stop`を再実行してください。`status`、回収、再接続、終了が新しいGPUを確保することはありません。

GPU側のモデル準備イベントは、取得したログのworker-events.jsonlへ保存します。bootstrap.logの依存導入時間と合わせて準備の所要時間を確認できます。画像の初回生成時間は履歴の生成開始・完了時刻から確認してください。

## プロンプトをファイルから指定する

起動済みセッションで実行します。テキストはUTF-8で保存し、BOMも使用できます。

```powershell
komaquill generate --prompt-file positive.txt --candidates 2
```

プロンプトは直接指定にも対応します。負プロンプトはWAIだけで使用できます。

```powershell
komaquill generate --prompt "a blue umbrella" --candidates 2
```

対象別の説明を含める場合はJSONを使用します。

```json
{
  "format_version": 4,
  "prompt": "rainy street",
  "subjects": ["woman holding a blue umbrella", "small gray kitten"]
}
```

```powershell
komaquill generate --input-file image-input.json --model klein-4b --candidates 2
```

直接指定とprompt-fileは併用できません。input-fileは他のプロンプト指定と併用せず、profile・seed・candidatesと組み合わせます。空のプロンプトはエラーです。品質用の既定文は別に適用します。

結果には候補数と保存先を表示します。ブラウザーや画像を自動で開きません。候補数の増加はGPUメモリを消費します。メモリ不足では条件を自動変更せず、ログを確認して少ない候補数で新しい生成を行います。

## 9BとColabの使用量

```powershell
komaquill usage
komaquill start --model klein-9b --gpu A100 --high-ram
komaquill generate --model klein-9b --prompt "an opaque red book inside a blue bag"
komaquill open --model klein-9b --view app
komaquill open --model klein-9b --operation edit --view graph
komaquill stop
```

利用条件と取得済みモデルを確認してから起動してください。既定モデルは4Bです。起動済みGPUのモデルを切り替える場合は、起動時に両方のプロファイルを準備します。GPUとHigh RAMは次の起動で指定します。自動的なGPU変更はありません。

`usage --json`は観測時刻を含む型付きJSONを返します。照会失敗時はエラーを返し、以前の観測や残量0へ置き換えません。
