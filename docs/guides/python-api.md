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
