# ストーリーから画像を生成する

## 準備

UTF-8のストーリーファイルとLLM追加依存を使用します。ソース用setupは追加依存も導入します。wheelの場合は導入時に `[llm]` を指定します。画像の生成には、同じ作業フォルダーで起動済みのセッションが必要です。

既定のLLMはLM Studioの4Bです。Local Serverを起動し、[既定値](../../src/komaquill/resources/stories/defaults.toml)にあるモデルが一覧にあることを確認します。実LLMの呼び出しには毎回 `--allow-real-llm` が必要です。9BやFakeへの自動切替はありません。

## LLMを選ぶ

4Bと9Bは接続プロファイルで切り替えます。実LLMの利用許可は、モデル選択とは別に毎回必要です。

```powershell
komaquill stories plan ./story.txt --llm-profile lmstudio-9b --allow-real-llm
komaquill stories plan ./story.txt --llm-profile lmstudio-4b --allow-real-llm
```

LM Studioで使用中のモデルをアンロードし、選択したモデルをロードしてから実行します。上記の引数はLM Studioの常駐モデルを管理しません。画像入力にはモデル本体と対応する画像用ファイルの両方が必要です。9Bを指定して失敗した場合も、4Bへ自動では切り替えません。

作業フォルダーで選択を固定する場合は、komaquill.tomlの既存のstoryテーブルへ設定します。

```toml
[story]
llm_profile = "lmstudio-9b"
```

優先順位はCLI／Pythonの指定、環境変数、作業設定、同梱既定値です。環境変数はKOMAQUILL_STORY_LLM_PROFILE、Python APIはllm_profileを使います。同梱既定はlmstudio-4bです。VS Codeの実LLM入口もこの選択を使います。動作条件と精度は[比較手順](llm-comparison.md)と[測定結果](../verification/results/llm-comparison.md)を確認してください。

## 本文と画像を順に作る

```powershell
komaquill start
komaquill stories generate ./story.txt --allow-real-llm
```

既定は自動選択、初期対応は1〜8枚です。それぞれ異なる場面の主画像で、コマ枠や吹き出しは作りません。保存が完了した場面から、日本語の本文と画像の保存先を表示します。画像ビューアーやブラウザーは自動起動しません。

generateは接続と画像用設定を事前確認し、利用できない場合はLLMを呼びません。GPUの起動前に計画を確認する場合は次の順で実行します。

```powershell
komaquill stories plan ./story.txt --allow-real-llm
komaquill start
komaquill stories render STORY_ID
```

planが表示した実際のIDをSTORY_IDへ指定します。planはGPUへ接続しません。storiesの操作ではGPUを新規確保しません。

## 条件の変更と再開

```powershell
komaquill stories render STORY_ID --image-profile pony --seed 42
komaquill stories resume RENDER_ID
komaquill stories resume RENDER_ID --retry-failed
```

renderは計画を変えず、新しい生成試行を作ります。生成セット、場面ごとのseedと生成IDを保存します。別の生成セットを使う場合はstart時にも選択して準備してください。品質タグ、ネガティブプロンプト、画質設定は[生成セット](../current/presets.md)で管理し、LLMには生成させません。

resumeは元の条件を維持し、保存済みの場面を再生成しません。受付不明の要求も自動再送しません。確定した失敗は通常は飛ばし、retry-failedを指定した場合だけ新しい要求として試します。必要なモデルと生成セットの互換性を確認し、再開では保存済みの条件を使用します。現在のステップ数など、無関係な既定値変更だけでは拒否しません。

Ctrl+Cは次の場面の開始とCLIの待機を止め、終了コード130を返します。送信済みの1枚は監視が回収します。GPUの終了には別途stopを実行します。部分失敗は終了コード1を返して対象と再開方法を表示します。通信や保存の失敗では追加送信を止めます。

## 本文・画像・進捗を確認する

```powershell
komaquill stories list
komaquill stories show STORY_ID
komaquill stories show STORY_ID --render-id RENDER_ID
komaquill stories show STORY_ID --follow --json
```

既定は最新の生成試行です。showはローカル記録だけを読みます。followは変更時に表示を更新し、全場面の保存後に終了します。未完了・失敗が残る場合はCtrl+Cで待機を終えられます。json単独はJSON、followとの併用はJSON Linesです。生成操作の進捗と診断は標準エラー、構造化出力は標準出力へ分けます。

出力には順序、本文、描く瞬間、人物・場所、原文参照、状態、画像キーと検証情報があります。将来の画面も同じ状態取得を使えます。画像の実体は作品の生成試行内へ保存し、生成IDと保存キーで関連付けます。

## 接続と指示を変更する

`komaquill.toml`でプロファイルを定義します。

```toml
[story]
llm_profile = "local"
image_profile = "pony"

[llm_profiles.local]
provider = "openai-compatible"
model = "your-model-id"
base_url = "http://127.0.0.1:1234/v1"
require_model_match = true
```

対応するCLI引数、KOMAQUILL_STORY_環境変数、作業設定、パッケージの既定値の順に優先します。llm-profileとimage-profileは操作ごとに選べます。LLM設定はGPUの起動設定から分離しています。instructions_fileでUTF-8の指示ファイルを指定できます。

外部接続はHTTPSを使用します。key_envへ明示した環境変数名、またはcredential_refへ資格情報ストアの参照名を指定します。資格情報のサービス名はkomaquill.llmです。キー自体をTOML、CLI引数、URLへ書きません。外部トレースは無効です。各接続先・モデルの構造化出力対応は別途確認が必要です。

待機時間、出力上限、修正回数、枚数の正本は[既定値](../../src/komaquill/resources/stories/defaults.toml)です。SDKの自動再試行は無効です。不正な計画や打切りを成功として扱いません。

## Fakeと保存

```powershell
komaquill stories plan ./src/komaquill/resources/stories/examples/rainy-cat/source.md --llm-profile fake --images 4
```

FakeListLLMはこのサンプルと対になった自動選択と固定1〜8枚の同梱応答を返します。任意ストーリーの理解結果には使えません。日常の品質ゲートはFakeと擬似ComfyUIで検証します。

作品は.artifacts/data/stories/作成日-元ファイル名-短縮IDへ保存します。request.jsonが原文、planningが呼び出しごとのスキーマ・修正指示・可視出力、plan.jsonが不変の確定計画、rendersが生成試行です。原文と指示・出力は非公開の作品データとして保持します。

Fakeの用途はverificationです。実機検証でもKOMAQUILL_STORY_PURPOSE=verificationとKOMAQUILL_STORY_CASE_IDで用途を記録できます。[AI分析用出力](evaluations.md)には作品と生成の対応を含めます。

人物の完全な同一性、長編の分割計画、自動採点、改善案の自動適用は対応していません。本文と画像を見比べて確認してください。

## 枚数と分析用出力

枚数の指定を省くと、LLMが出来事と描画方針から場面数を選びます。`--images 4`で固定し、`--images auto`で作業設定の固定値を自動選択へ上書きできます。固定枚数に合わせた場面の水増しは成功として扱いません。

`komaquill stories export STORY_ID`は計画、原文、呼び出し前の入力、成功・失敗の応答、使用量、画像参照をJSONへ出力します。失敗した計画も作品IDで出力できます。原文と本文を含むため、外部AIへ渡す前に内容を確認してください。この操作で外部への送信は行いません。

比較では固定枚数と自動枚数を分け、入力と画像条件を固定します。総トークンには修正呼び出しも含めます。意味の保持と画像の品質は、構造検査とは別に評価します。

## 作品フォルダーを開く

保存先は`.artifacts/data/stories`です。このフォルダーのREADMEに、計画未完了・計画済み・画像未完了・保存完了の作品を表示します。CLIでは`stories list`で同じ一覧を確認できます。作品フォルダーのREADMEから生成試行のstory.mdを開くと、本文と画像を場面順に確認できます。各試行のimagesには連番画像、runsには生成条件と履歴があります。再試行しても以前の画像を上書きしません。

本文表示の再構築には`komaquill stories show STORY_ID --refresh`を使用します。画像や生成条件を再生成せず、保存済みの記録からMarkdownとmanifestを更新します。

以前の配置から更新した作業場所では、セッションを停止して`komaquill migrate --dry-run`で移動対象を確認し、`komaquill migrate`を実行してください。中断した場合は同じコマンドで再開できます。作品・生成IDと画像・履歴の原本は保持します。


原文、LLMによる本文の再構成、描画設定は別の情報です。外見と描く瞬間は画像のための提案であり、原文の事実として自動検証していません。exportのdata_rolesはこの区別を示します。supplementsはLLMが申告した補足なので、空でも「補足が存在しない」とは断定しません。内容を公開する前には原文と計画を確認してください。

## 比較用の要約と完全な記録

```powershell
komaquill stories export STORY_ID
komaquill stories export STORY_ID --detail full
komaquill stories export STORY_ID --render-id RENDER_ID --detail summary
```

既定のsummaryは原文、本文と画像の対応、生成条件、評価、使用量、失敗をJSONへまとめます。fullは指示・スキーマ・全応答・実行グラフをハッシュで参照する完全な記録を加えます。画像バイナリは含めません。ファイルへの保存は呼び出し側で標準出力を保存してください。

画像と原本のworkspace_pathは作業フォルダー相対です。論理キーを物理パスへ連結しません。JSONだけで画像を見たことにはならないため、画質評価では参照先の画像も確認します。

## 失敗した場面を確認する

```powershell
komaquill stories show STORY_ID --follow --json
komaquill stories resume RENDER_ID --retry-failed
```

followは失敗・回収不能を含む終端でも終了します。失敗した要求IDと再開方法を表示し、非ゼロ終了します。受付不明や未開始は終端と扱いません。Ctrl+Cで追跡を中断できます。

閲覧用ファイルだけの修復にはshowの--refreshを使用します。画像と履歴の保存成功は維持されます。破損した履歴を未送信と推測して再送しません。

## 閲覧用ファイルだけを再構築する

画像が保存できてもREADME・story.md・manifestを更新できない場合、後続場面の生成は続き、最後に非ゼロ終了と次の操作を表示します。

```powershell
komaquill stories show STORY_ID --render-id RENDER_ID --refresh
```

ファイルの使用中状態や保存先の権限など、表示された問題を解消してから実行します。画像を生成し直す操作ではありません。--jsonでは保存状態のJSONを標準出力、再構築の案内を標準エラーへ出力します。

分析用JSONのunreadable_evaluation_idsは、読めない評価記録の識別子です。空でない場合は評価が一部欠けているため、全評価を反映した比較として扱わないでください。

## 物語用の画像生成セットを選ぶ

作業設定で使用するセットを有効にしてからstartします。起動済みの場合はstop後にstartしてください。既存の作業設定は自動で書き換えません。

```toml
profiles = ["pony", "pony-story"]

[story]
image_profile = "pony-story"

[presets.pony-story]
quality_enabled = true
```

ストーリー操作の`--image-profile pony-story`でも選べます。品質用の正負プロンプトは既定で有効です。無効にする場合はquality_enabledをfalseへ変更して接続を開始し直します。生成試行を再開するときは、作成時に保存した品質設定が維持されます。

通常の全体プロンプトと領域指定の画質は実画像で比較します。領域指定だけで顔の完全な同一性や複雑な接触を保証するものではありません。
