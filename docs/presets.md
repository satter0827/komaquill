# モデルとワークフローの管理

## 設定の場所

`src/komaquill/resources/presets.toml`は、配布する資源と生成プリセットの定義です。`assets`に配布元・固定リビジョン・SHA256・ファイル名・用途・ライセンスを、`presets`にワークフローと生成条件を記載します。用途は`checkpoints`、`loras`、`upscale_models`です。専用のダウンロード機構や計算ノードは追加しません。

ワークフローの接続は同じresources内の標準JSONで管理します。API用の`*-api.json`と画面用JSONを対にします。生成条件は同じ適用処理を通し、CLIと画面の差を構造テストで確認します。LoRAには標準のLoraLoaderModelOnly、画像拡大にはUpscaleModelLoaderとImageUpscaleWithModelを使います。

## 作業フォルダーで変更する

`komaquill.toml`には必要な差分だけを書きます。

```toml
profiles = ["pony", "pony-preview"]

[presets.pony]
steps = 30
cfg = 6.0
sample_prompt = "a quiet lake, illustration"
```

優先順位は、対応するCLI引数、環境変数、作業設定、パッケージの既定値です。`KOMAQUILL_PROFILES`はJSON配列で指定します。設定はstartで確定し、生成・画面・回収・終了で同じ値を使います。変更はstop後のstartから反映します。

`start --profile pony --profile pony-preview`は、その接続で使うプリセットの一覧を置き換えます。`open --profile`と`generate --profile`は、準備済みの中から選択します。未選択の資源を生成時に自動取得しません。

## 資源を追加する

同じ構成のモデルへ替える場合は、新しいassets定義を追加し、プリセットのcheckpointを指定します。Hugging Faceは固定リビジョンと実ファイルのSHA256、GitHub ReleaseなどはHTTPSの固定URLとSHA256を使用します。モデルの重みはGitやwheelへ含めません。

別の接続構成を使う場合は、標準JSONの対と、その入力に対応する適用処理を追加します。既存の接続を説明するだけの独自DSLや、汎用プラグイン機構は追加しません。追加した資源の形式、モデル系列、CLIP/VAE、LoRAの対応系列を確認し、GPUを使わない構造検査と実機生成を行ってください。

## 配置と準備時間

ローカル正本は`.artifacts/models/資源名/`です。`models fetch --profile pony-preview`で事前取得できます。同じPonyを使う複数のプリセットでも重みは重複取得しません。正常な既存ファイルはSHA256確認後に再利用します。

Colabは固定した配布元から一時領域へ直接取得します。PCから毎回大きな重みを送信しません。Colab内の再接続では同じ一時キャッシュを使えますが、新しいランタイムでは取得し直します。Google Driveへコピーしません。

準備ログのmodelイベントは、cache_verified、download_started、download_verifiedを区別します。elapsed_secondsはSHA256確認を含む所要時間です。API接続の準備完了と、初回生成時の重み読み込み完了は別です。準備完了後の最初の画像は、以後の画像より時間がかかることがあります。

## 利用条件

- [Pony V6 XLの配布ページ](https://civitai.com/models/257749/pony-diffusion-v6-xl)の作者条件を確認してください。取得元の[Hugging Face](https://huggingface.co/AstraliteHeart/pony-diffusion-v6)にあるライセンス表示だけで、商用サービスへの組込みが無条件に許可されるとは判断しません。
- [Hyper-SD](https://huggingface.co/ByteDance/Hyper-SD)はSDXL用ファイルを固定しています。ライセンスはモデル系列ごとに異なるため、SDXLの項目を確認してください。
- [Real-ESRGAN](https://github.com/xinntao/Real-ESRGAN)は作者のReleaseからanime 6Bを取得します。

プレビューは少ないステップで確認するための選択肢です。絵柄や構図の一致は保証しません。拡大は生成後の画素拡大で、細部を作り直す拡散処理とは別です。
