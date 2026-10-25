# モデルと生成セット

## 定義の配置

配布する生成セットはresources/presets/ID/に置きます。preset.tomlが生成条件・モデル参照・入力対応、workflow.jsonが標準画面、workflow-api.jsonが標準API用のグラフです。共有モデルの配布元・固定リビジョン・SHA256・用途・ライセンスはresources/models/に置きます。

Pony、物語用の領域指定、8ステッププレビュー、画像拡大、SD 1.5の5セットを同じ形式で管理します。モデルの重みはGitとwheelに含めません。

## 利用者のセット

作業フォルダーのpresets/ID/に同じ3ファイルと、必要に応じてplanning.tomlを置き、komaquill.tomlのprofilesへIDを追加します。同名のセットはフォルダー一式で置き換えます。不足ファイルを同梱版から補うことはありません。

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

別の構成では標準ComfyUIで両JSONを作成し、その入力対応を記載します。CLIが扱う入力は現在の生成コマンドの範囲です。対象別入力は登録された領域指定セットで扱います。任意の計算を表現する独自言語はありません。詳細な編集にはNode Graphを使用します。

## 保存と再利用

モデルは.artifacts/data/models/用途/SHA256/へ保存します。固定配布元と検証済みの台帳を使って再利用します。モデル準備と素材保存の範囲は[準備手順](../guides/preparation.md)と[素材保全](../guides/preservation.md)を参照してください。

Pony、Hyper-SDXL、Real-ESRGANの利用条件は各モデル定義のlicenseとlicense_urlを確認してください。新しいモデル系列・LoRA・ワークフローは互換性と実機生成を確認してから登録します。

## パラメーターを調整する

通常は作業フォルダーのkomaquill.tomlの`[presets.対象ID]`でsteps、cfg、width、height、sampler、scheduler、negativeなどを変更します。セットごとの値を起動時に上書きし、全セット共通の値を暗黙に優先させません。profilesは使うIDと既定の順序を選び、生成条件そのものではありません。

構成・モデル参照・入力対応も変更する場合は、同梱のセットフォルダーを作業フォルダーのpresets/IDへコピーし、一式を編集します。変更後はstopとstartで再検証します。個々の生成でseedや正負プロンプトを変える場合はgenerateの引数、実行中の詳細編集はNode Graphを使います。

単画像CLIは正負の直接入力、UTF-8ファイル、JSON入力を扱います。JSONとPython APIのsubjects、ストーリー生成は最大2対象の入力に対応します。入力方法は[操作](../guides/usage.md)を参照してください。


## ストーリーの描画方針

各セットのplanning.tomlは、主な被写体、単純な動作、共通画風などの計画方針です。画像グラフとは分離し、GPUへ転送しません。ファイルを省略した利用者セットは同梱の基本方針を使用します。同名セットのグラフを部分的に補う動作はありません。

作業設定の`[story.planning]`からstyleとguidanceを変更できます。プロファイルと描画方針の部分上書きは、指定した項目だけへ適用します。LLMにはモデル固有の品質タグを生成させません。

## 品質プロンプト

`quality_enabled`は既定で有効です。`positive_prefix`は正の品質用既定文、`negative_prefix`は負の品質用既定文です。有効時は利用者の入力と合成し、無効時は両方の既定文を付けません。`negative`と生成時のnegative引数は利用者が指定する内容です。引数を省略するとセットのnegativeを使い、空文字列ならその部分だけを空にします。品質用の負の既定文も外す場合はquality_enabledを無効にします。

Ponyのscore系タグを他のモデルへ自動転用しません。品質タグはLLMの出力に含めず、画像入力の共通処理で適用します。原文の動物を排除するような指定を既定へ追加しないでください。

## 物語用の領域指定

`pony-story`はComfyUI公式Area Compositionの第1段をPony向けに調整した生成セットです。モデルはponyと共用します。出典、固定コミット、元画像のSHA256と変更点はセット内のprovenance.toml、利用条件はLICENSE.upstreamにあります。

subjectsは最大2件で、左から右の順です。1件ならsingle_region、2件ならregionsの左右配置を使います。座標と寸法は画像全体に対する割合、strengthは条件付けの強度です。未使用の領域は強度を0にします。対象なしの通常プロンプトも使用できます。自由な領域数や式による入力定義は扱いません。

App ModeとNode Graphは同じ標準グラフから作成し、必要な入力を事前設定します。画面から編集した条件は、実行されたグラフを正本として保存します。登録していない外部ワークフローへの品質タグ適用は行いません。

生成ごとのCompiledImageInputは、元の指定・品質用既定文・適用済みの正負入力・対象別入力・構図・プリセットのハッシュと変換処理の版を持ちます。過去の入力を現在の既定値で作り直しません。

実画像比較では対象数・種別・小物の所有関係の基準を満たしていないため、pony-storyは選択式です。既定はponyを維持します。詳細は[検証結果](../verification/results/story-image-inputs.md)を参照してください。

利用者の標準グラフで負の品質文を別に結合する場合、negative_prefixには適用する品質文、negativeには利用者の指定、negative_delimiterには必要な区切り文字を渡します。negative_prefixを独立して結合しない構成では、negativeが合成済みの負の入力です。APIと標準画面へ同じ値を設定し、品質の無効化時に品質文を残しません。

## 候補の標準バッチ

batch_sizeは一つの要求で生成する候補数です。CLIとPythonのcandidatesは要求ごとに上書きし、起動済みセッションの既定値を変更しません。既定と要求上限の正本は[preset.toml](../../src/komaquill/resources/presets/pony/preset.toml)と[運用設定](../../src/komaquill/resources/settings/defaults.toml)です。

primary_output_nodeは最終候補を出す標準SaveImageノードです。画像拡大セットは拡大後を主出力とし、拡大前も補助出力として保存します。標準画面のCandidatesは潜在画像のbatch_sizeを編集します。キューへ同じ要求を繰り返すbatch_countとは用途が異なります。

正負の品質文と利用者入力は標準の文字列ノードで結合します。負の入力は標準のRegexReplaceで空入力時の区切りを除き、App Modeで入力を変更した場合も品質文との区切りを保ちます。条件はAPIと画面の両グラフへ同じ値を適用します。

利用者の旧セットで候補数を指定するには、batch_sizeの入力対応とprimary_output_nodeを追加してください。未対応のセットへ明示したcandidatesを無視せず、送信前にエラーにします。候補の役割が不明な保存画像を0候補として表示しません。
