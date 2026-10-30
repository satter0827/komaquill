# Python API

公開入口は`komaquill.api`です。CLIとHTTPも同じClientを使用します。importで通信、認証、作業ファイルの作成、プロセス起動、表示を行いません。

```python
from uuid import uuid4
from komaquill.api import Client
from komaquill.conversations.models import ConversationInput, SendInput

client = Client.initialize("./my-work")
conversation = client.conversations.create(ConversationInput(title="My conversation", llm_profile="fake"))
operation = client.conversations.send(conversation.id, SendInput(operation_id=uuid4(), text="Hello"))
saved = client.conversations.get(conversation.id)
```

実モデルは会話作成時にプロファイルを選び、各送信で`allow_real_llm=True`を指定します。サンプルは固定入力専用です。

`conversations`は作成・一覧・取得・送信・状態・コマンド・再開、`operations.get`は操作の保存済み状態を取得します。`images.submit/get/collect`は送信・取得・回収、`images.generate`は同じ送信処理と完了待ちを組み合わせます。

`runtime`はGPUの開始・状態・停止を明示的に扱います。Clientを閉じても独立したGPUや監視を停止しません。対話認証はCLIで完了させます。`archive`は旧記録の一覧・取得・書き出しを読み取り専用で扱います。

失敗は安全な`KomaquillError`とエラーコードで返し、通常処理がコンソール表示や終了コードを決めません。保存場所とHTTPの契約は[現在の構成](../current/web.md)を参照してください。

## プリセットと好み

`Client.players`、`Client.characters`、`Client.stories` で一覧・作成・取得・版を指定した編集・複製・削除を行います。入力には公開型 `PlayerInput`、`CharacterInput`、`StoryInput` を使います。`Client.library.preferences()` と `save_preferences()` で表示・生成の好みを保存します。

会話開始は `Client.library.start(ConversationStart(...))` に3種類の `PresetRef(id=..., revision=...)` を渡します。`llm_profile` と `locale` は開始時に指定します。開始済みの会話には解決済みの内容が保持されます。

下書き・代表画像・バナーは `CreationRequest` を `Client.library.accept()` へ渡し、新規受付の場合だけ `execute()` します。`job()` は保存済みの進捗を取得し、同じIDの再確認で生成を送り直しません。単発画像の日本語入力には `llm_profile` と `allow_real_llm=True` を指定します。既存DBを更新する場合は停止後に `Client.migrate()` を明示実行し、返された検証記録を確認します。
