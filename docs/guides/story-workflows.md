# 参照画像とマスクを使う

## 起動

必要な生成セットをまとめて選びます。起動済みの場合は先にstopで終了し、準備後にdoctorでモデルとノードを確認します。

3系統は実験用です。保存・実行の確認と、4場面の画質の受入結果は[検証記録](../verification/results/staged-workflows.md)を確認してください。

```powershell
komaquill start --profile story-reference --profile story-compose --profile story-inpaint
komaquill doctor
```

GPUの確保はstartだけで行います。外部ComfyUIでは必要なノードとモデルを事前に用意してください。

## 局所修正

次の内容をimage-input.jsonとして保存します。素材のパスはこのJSONを基準に解決します。

```json
{
  "format_version": 2,
  "prompt": "a woman shelters a gray kitten with a single blue umbrella, soft illustration",
  "negative": "extra umbrellas, floating objects",
  "parameters": {"reference_weight": 0.2, "denoise": 1.0},
  "inputs": {
    "reference": "references/mio.png",
    "base": "bases/scene-2.png",
    "mask": "masks/scene-2.png"
  }
}
```

マスクは編集元と同じ寸法のグレースケールPNGを使います。白い範囲を変更し、黒い範囲を保持します。色付き画像や透過の意味が曖昧なマスクは受け付けません。

```powershell
komaquill generate --profile story-inpaint --input-file image-input.json --candidates 2
```

入力素材はローカルへ保全されます。元ファイルを後から変更しても、受付済みの要求へ反映しません。

## 場面ごとの指定

render.jsonのscenesへ、計画のすべての場面を指定します。sceneは本文の場面順です。次は一場面の計画に対応する例です。四場面ならsceneを1から4まで含めます。

```json
{
  "format_version": 1,
  "scenes": [
    {
      "scene": 1,
      "profile": "story-reference",
      "inputs": {"reference": "references/mio.png"},
      "parameters": {"reference_weight": 0.6}
    }
  ]
}
```

story-composeにはreference、base、person_mask、cat_reference、cat_maskを指定します。referenceは人物と傘、cat_referenceは猫の外見です。対象別説明は計画を使用し、必要な場合だけsubjectsで二つの説明を指定します。

story-inpaintにはreference、base、maskを指定します。prompt・negative・subjectsは省略すると計画と共通指定を使用します。parametersはセットが宣言した名前・型・範囲で上書きできます。

```powershell
komaquill stories render STORY_ID --render-file render.json --seed 31415 --candidates 2
komaquill stories show STORY_ID
komaquill stories resume RENDER_ID
```

全場面の素材と条件を検証してから画像要求を送ります。保存できた場面から本文と画像の場所を表示します。再開時に元のJSONや素材は読み直しません。条件を変更した場合はrenderで新しい試行を作成します。

## Python API

```python
from komaquill.api import Client, RenderConfig, SceneRenderConfig

client = Client("work")
reference = client.inputs.import_file("references/mio.png")
run = client.images.generate(
    "a woman under a blue umbrella",
    profile="story-reference",
    inputs={"reference": reference},
    parameters={"reference_weight": 0.2},
    candidates=2,
)

# Assign every position in the selected saved plan.
config = RenderConfig(
    scenes=(SceneRenderConfig(scene=1, profile="story-reference", inputs={"reference": reference}),)
)
```

マスクはkind="mask"で取り込みます。APIはコンソールへ出力せず、Runまたは場面の状態イベントを返します。本文と画像は作品のstory.md、対応表はmanifest.json、詳細条件はstories exportで確認できます。

## 画面と終了

```powershell
komaquill open --profile story-inpaint --view app
komaquill open --profile story-inpaint --view graph
komaquill collect
komaquill stop
```

App Modeでは素材・説明・負プロンプト・候補数、Node Graphでは工程の詳細を編集します。素材が未設定の場合はLoadImageで保存済み素材を選択します。保存済み実行グラフには実際の素材参照を保持します。

失敗した要求でも公開された途中画像は回収できます。途中画像の保存は生成成功を意味しません。必要ならその画像を新しい編集元として取り込み、別の試行で修正します。
