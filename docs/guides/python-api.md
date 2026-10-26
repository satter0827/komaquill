# Pythonから利用する

wheelを導入したPython環境から、CLIと同じ計画・生成・保存処理を利用できます。LLMを使用する場合は追加依存`komaquill[llm]`を導入します。Colab操作には`komaquill[colab-client]`が必要です。

## 作業場所を準備する

```python
from komaquill.api import Client

client = Client.initialize("./my-work")
```

既存の設定は上書きしません。初期化とClientの作成ではGPUを起動しません。既存の作業場所を使う場合は`Client("./my-work")`で開きます。

## 計画して画像を生成する

```python
from pathlib import Path
from komaquill.api import Client

with Client("./my-work") as client:
    text = Path("story.txt").read_text(encoding="utf-8-sig")
    plan = client.stories.plan(text, allow_real_llm=True)
    story_id = plan.request.story_id
    for event in client.stories.render(story_id):
        for scene in event.snapshot.scenes:
            if scene.primary_image is not None:
                image_path = client.artifact_path(scene.primary_image.key)
```

実LLMの許可は操作ごとに必要です。許可をClient全体へ保存しません。Fakeは同梱サンプルと一致する入力だけで利用できます。

画像生成には起動済みセッションが必要です。`stories.render`や`stories.generate`はGPUを確保しません。途中経過と完了は同じ型付きスナップショットで受け取ります。最新状態は`client.stories.show(story_id)`、再開は`client.stories.resume(render_id)`を使用します。

## 終了と失敗

Clientのコンテキストを終了してもGPUは停止しません。停止には`client.sessions.stop()`を使用します。Pythonからの起動は既存のGoogle認証を必要とし、既定では対話認証を行いません。

操作失敗は`KomaquillError`のcode・stage・retryableで判断します。中断はKeyboardInterruptとして扱え、ストーリー操作では保存済みの作品・生成試行のIDを保持します。利用者への表示とプロセスの終了コードは呼び出し側で決めます。

[CLIの操作](stories.md)と[保存の契約](../current/stories.md)も参照してください。


## 一覧と分析用出力

`client.stories.list()`は作品名・ID・枚数・保存状態を返し、読めない作品はunreadable_idsへ分けます。`client.stories.export(story_id)`は、原文、場面、生成条件、成功・失敗、使用量と画像の対応を型付きで返します。指示・スキーマ・可視応答を含む原本はdetail="full"で取得します。書き出しや外部AIへの送信は呼び出し側の明示操作で行います。

`client.stories.refresh(story_id)`は保存済みの記録からREADME・story.md・manifestを再構築します。`client.migrate(dry_run=True)`は移動対象、`client.migrate()`は停止確認と検証を伴う移行・再開を行います。どちらもGPUを新規確保しません。

公開型はkomaquill.apiから必要なものだけをimportします。内部実装のモジュールやファイル配置へ依存せず、画像には保存キーとartifact_pathを使用します。wheelにはpy.typedを含めているため、利用側の型チェックから型付き結果と逐次イベントを参照できます。


比較対象は保存済み画像のある作品だけへ絞らず、一覧のunplannedも含めてexportします。計画がない場合はstoryがNoneになり、成功・失敗の呼び出し記録はplanningへ残ります。使用量を集計するときは修正呼び出しと拒否した計画を含め、不明値をゼロへ置き換えません。

## 分析用出力の詳細度

戻り値は版付きのStoryEvidenceです。既定はsummaryで、完全な原本が必要な場合にfullを指定します。

```python
evidence = client.stories.export(story_id)
print(evidence.planning.status)
print(evidence.planning.usage.total_tokens)

full = client.stories.export(story_id, detail="full")
for record in full.records:
    original_path = client.workspace / record.workspace_path
```

summaryとfullで対象・条件・集計は同じです。full.objectsは重複を省いたJSON内容、recordsは原本参照とバイト列のSHA256を保持します。画像はartifact_pathまたはworkspace_pathで解決します。

completeは保存成功、finishedは失敗を含めた全場面の終了です。逐次イベントはprogressとfinishedで、終了してもcomplete=falseなら失敗した場面を確認します。

## 閲覧用ファイルと任意評価の失敗

StoryEvent.publication_failureは、画像の保存状態とは別の閲覧用更新の結果です。失敗しても後続の場面を生成します。最後のイベントでこの値が残った場合は、原因を解消してclient.stories.refresh(story_id, render_id=render_id)で再構築します。保存済み画像を生成し直す必要はありません。

分析用出力のunreadable_evaluation_idsが空でない場合は、一部の評価記録を取得できていません。生成の原本や参照する過去の試行が破損している場合は、黙って省略せずKomaquillErrorを返します。

## 対象別の画像入力

起動済みセッションでkleinを有効にしてから実行します。本文を伴う作品はstoriesを使い、単画像の対象別入力はimages.generateのsubjectsへ指定します。説明は全体へ結合し、空間配置を保証しません。

```python
run = client.images.generate(
    "rainy street beside a shop awning",
    profile="klein",
    subjects=(
        "human woman, yellow raincoat, holding a blue umbrella",
        "small gray kitten, four legs, sitting under the umbrella",
    ),
    seed=42,
)
applied = run.compiled_input
assert applied is not None
print(applied.positive, applied.negative)
```

CompiledImageInputはkomaquill.apiから参照できます。利用者が渡した内容と品質プロンプトを適用した条件を確認できます。画面から生成した履歴では、対応する利用者入力を確認できないためcompiled_inputはNoneです。実行条件は保存されたグラフから確認します。

## 複数候補

```python
run = client.images.generate("a blue umbrella", candidates=2)
for name in run.images:
    metadata = run.image_metadata[name]
    print(metadata.role, metadata.output_node, metadata.batch_index)

for event in client.stories.render(story_id, candidates=2):
    for scene in event.snapshot.scenes:
        for candidate in scene.candidates:
            image_path = client.artifact_path(candidate.key)
```

単画像生成は一つのRunに候補を保存します。Pythonからは文字列とsubjectsを渡し、ファイル読み込みを呼び出し側で行えます。CLIから読み込んだファイルの内容・元の名前・SHA256はinput_sourcesへ保存されます。明示的なcandidatesはプリセットを要求単位で上書きし、再開では保存済みの条件を維持します。

## 素材と場面別の生成セット

client.inputs.import_fileは画像または白が編集対象のマスクを保存し、InputArtifactを返します。images.generateのinputsへ渡し、parametersで宣言済みの工程設定を上書きできます。全場面の指定にはRenderConfigとSceneRenderConfigを使い、stories.renderのrender_configへ渡します。[素材入力と場面指定](story-workflows.md)に例と制約を記載しています。

負プロンプトの非空指定は送信前に拒否します。参照画像の編集は[編集手順](story-workflows.md)を参照してください。
