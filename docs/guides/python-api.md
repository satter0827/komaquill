# Python API

長時間動くアプリケーションではClient.configure()で設定を固定します。LLMは同じClient内の会話・下書き・要約・英訳で設定を共有し、初回利用後の設定ファイル変更は次のClientから反映されます。保存済み作品の閲覧、画像の回収、GPU終了はLLM設定を必要としません。[設定の有効期間](../current/library-delegation.md#設定の解決と有効期間)を参照してください。

公開入口は`komaquill.api`です。CLIとHTTPも同じClientを使用します。importで通信、認証、作業ファイルの作成、プロセス起動、表示を行いません。

```python
from uuid import uuid4
from komaquill.api import Client, ConversationInput, SendInput

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

`Client.player.get/update`で1件の自分を取得・編集します。`Client.characters`と`Client.stories`で一覧・作成・取得・版を指定した編集・複製・削除を行います。入力には公開型 `PlayerInput`、`CharacterInput`、`StoryInput` を使います。`Client.library.preferences()` と `save_preferences()` で表示・生成の好みを保存します。

会話開始は `Client.library.start(ConversationStart(...))` に3種類の `PresetRef(id=..., revision=...)` を渡します。`llm_profile` と `locale` は開始時に指定します。開始済みの会話には解決済みの内容が保持されます。

下書き・代表画像・バナーは `CreationRequest` を `Client.library.accept()` へ渡し、新規受付の場合だけ `execute()` します。`job()` は保存済みの進捗を取得し、同じIDの再確認で生成を送り直しません。単発画像の日本語入力には `llm_profile` と `allow_real_llm=True` を指定します。既存DBを更新する場合は停止後に `Client.migrate()` を明示実行し、返された検証記録を確認します。

## 保存と画像を一緒に受け付ける

画像も作る場合はlibrary.save_presetを使う。画像生成先の起動・接続は別に行う。

```python
from komaquill.api import CharacterInput, PresetSaveOptions

result = client.library.save_preset(
    "character",
    CharacterInput(name="Mira", age="60", gender="man", appearance="Short silver hair", initial_clothing="Black apron"),
    PresetSaveOptions(operation_id=uuid4(), allow_real_llm=True),
)
```

result.presetは保存済み本文、result.jobは画像の操作、result.image_errorは画像受付の失敗を表す。更新ではtarget_idとexpected_revisionを渡す。再確認は同じ操作IDと同じ入力を使い、明示的な再生成はimage_policyのalwaysを指定する。本文だけを保存するときはnoneを使う。HTTPはcharacters/saveとstories/saveで同じ処理を呼ぶ。

ConversationInputのuser・character・scenarioは、公開するPersonDefinition・CharacterDefinition・StoryDefinitionを使う。initial_stateはConversationStateを使い、自分と相手の服装・持ち物を別に保持する。長い会話の要約は自動保存され、元の本文と現在状態は維持される。

開始時のplace・goalをinitial_stateへ指定しなければ、scenarioの同じ項目を使う。initial_stateへ明示した値は空文字列も含めて優先する。解決した初期状態は会話に保存される。現在状態の変更には行動を使い、開始状態への全置換は提供しない。プリセット経由とHTTPも同じ処理を使う。

旧状態を安全に変換できない会話はarchive.conversationsとarchive.conversationで読む。元の記録を新しい会話として推測変換しない。

画像の用途を直接指定する場合は `client.images.submit(..., purpose="banner")` または `generate()` を使います。用途の型は公開APIの `ImagePurpose` です。用途別の生成条件は生成セットで管理し、未指定の単発画像には追加しません。再開は保存済みのRunを回収し、条件を再適用しません。

## 保存済み画像の参照を復旧する

アプリを閉じた状態で候補を確認し、確認IDを指定して適用します。候補確認はDBを変更しません。条件、保留理由、バックアップと再実行は[画像参照の復旧](../current/image-recovery.md)を参照してください。

```python
inspection = client.inspect_image_links()
result = client.repair_image_links(inspection.id)
```

## 自分の行動を確定する

```python
from komaquill.api import ActionRequest, MoveAction

saved = client.conversations.get(conversation.id)
record = client.conversations.act(
    saved.id,
    ActionRequest(
        operation_id=uuid4(),
        expected_revision=saved.revision,
        action=MoveAction(destination="Beach"),
        locale="en",
        allow_real_llm=True,
    ),
)
reaction = client.operations.get(record.reaction_id)
```

行動は返答より先に保存する。反応の失敗と行動の確定を区別し、同じ行動をやり直さず同じ操作IDで結果を確認する。保存済みの行動はconversations.actions、会話中の案はConversation.proposalsで取得する。古い版の案は適用しない。詳細は[自分の行動と現在の場面](../current/player-actions.md)を参照する。
