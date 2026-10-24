# モデルと生成セット

## 定義の配置

配布する生成セットはresources/presets/ID/に置きます。preset.tomlが生成条件・モデル参照・入力対応、workflow.jsonが標準画面、workflow-api.jsonが標準API用のグラフです。共有モデルの配布元・固定リビジョン・SHA256・用途・ライセンスはresources/models/に置きます。

既定のPony、8ステッププレビュー、画像拡大、SD 1.5の4セットを同じ形式で管理します。モデルの重みはGitとwheelに含めません。

## 利用者のセット

作業フォルダーのpresets/ID/に同じ3ファイルを置き、komaquill.tomlのprofilesへIDを追加します。同名のセットはフォルダー一式で置き換えます。不足ファイルを同梱版から補うことはありません。

```toml
profiles = ["pony", "custom"]

[presets.custom]
steps = 30
cfg = 6.0
negative = "text, watermark"
```

対応するCLI引数、環境変数、作業設定、同梱の既定値の順に優先します。設定とグラフはstartで確定し、変更はstop後のstartから反映します。生成ごとの編集は確定済みのグラフを変更しません。

## 入力対応

preset.tomlのbindingsは、名前付きの生成条件をAPIのノード入力と画面のウィジェットへ適用する定義です。式や実行コードは扱いません。正負プロンプト、seed、checkpoint、保存先接頭辞は必須です。参照先と両グラフのノード種類・入力位置をGPU確保前に検証します。

別の構成では標準ComfyUIで両JSONを作成し、その入力対応を記載します。CLIが扱う入力は現在の生成コマンドの範囲です。独立した複数プロンプトや任意の計算を表現する独自言語はありません。詳細な編集にはNode Graphを使用します。

## 保存と再利用

モデルは.artifacts/data/models/用途/SHA256/へ保存します。固定配布元と検証済みの台帳を使って再利用します。モデル準備と素材保存の範囲は[準備手順](preparation.md)と[素材保全](preservation.md)を参照してください。

Pony、Hyper-SDXL、Real-ESRGANの利用条件は各モデル定義のlicenseとlicense_urlを確認してください。新しいモデル系列・LoRA・ワークフローは互換性と実機生成を確認してから登録します。
