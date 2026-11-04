# Python API

長時間動くアプリケーションではClient.configure()で設定を固定する。LLMは同じClient内の会話・下書き・記憶作成・英訳で設定を共有し、初回利用後の設定ファイル変更は次のClientから反映する。保存済み作品の閲覧、画像の回収、GPU終了はLLM設定を必要としない。[設定の有効期間](../current/library-delegation.md#設定の解決と有効期間)を参照する。

公開入口は`komaquill.api`とし、CLIとHTTPも同じClientを使用する。importで通信、認証、作業ファイルの作成、プロセス起動、表示を行わない。

```python
from uuid import uuid4
from komaquill.api import Client, ConversationInput, SendInput

client = Client.initialize("./my-work")
conversation = client.conversations.create(ConversationInput(title="My conversation", llm_profile="fake"))
operation = client.conversations.send(conversation.id, SendInput(operation_id=uuid4(), expected_revision=conversation.revision, text="Hello"))
saved = client.conversations.get(conversation.id)
```

実モデルは会話作成時にプロファイルを選び、各送信で`allow_real_llm=True`を指定する。サンプルは固定入力専用とする。

`conversations`は作成・一覧・取得・送信・状態・コマンド・再開、`operations.get`は操作の保存済み状態を取得する。`images.submit/get/collect`は送信・取得・回収、`images.generate`は同じ送信処理と完了待ちを組み合わせる。

`runtime`はGPUの開始・状態・停止を明示的に扱う。Clientを閉じても独立したGPUや監視は停止しない。対話認証はCLIで完了させる。`archive`は旧記録の一覧・取得・書き出しを読み取り専用で扱う。

失敗は安全な`KomaquillError`とエラーコードで返し、通常処理がコンソール表示や終了コードを決めない。保存場所とHTTPの契約は[現在の構成](../current/web.md)を参照する。

## プリセットと好み

`Client.player.get/update`で1件の自分を取得・編集する。`Client.characters`と`Client.stories`で一覧・作成・取得・版を指定した編集・複製・削除を行う。入力には公開型 `PlayerInput`、`CharacterInput`、`StoryInput` を使う。`Client.library.preferences()` と `save_preferences()` で表示・生成の好みを保存する。

会話開始は `Client.library.start(ConversationStart(...))` に3種類の `PresetRef(id=..., revision=...)` を渡す。`llm_profile` と `locale` は開始時に指定する。開始済みの会話には解決済みの内容を保持する。

下書き・代表画像・バナーは `CreationRequest` を `Client.library.accept()` へ渡し、新規受付の場合だけ `execute()` する。`job()` は保存済みの進捗を取得し、同じIDの再確認で生成を送り直さない。単発画像の日本語入力には `llm_profile` と `allow_real_llm=True` を指定する。既存DBを更新する場合は停止後に `Client.migrate()` を明示実行し、返された検証記録を確認する。

## 本文の保存と画像の生成

library.save_presetは本文だけを保存する。画像生成は保存済みの版に対して別の操作で行う。

```python
from komaquill.api import CharacterInput, PresetSaveOptions

result = client.library.save_preset(
    "character",
    CharacterInput(name="Mira", age="60", gender="man", appearance="Short silver hair", initial_clothing="Black apron"),
    PresetSaveOptions(operation_id=uuid4()),
)
```

result.presetは保存済み本文を表す。更新にはtarget_idとexpected_revisionを渡す。同じ操作IDと入力の再確認は保存結果を返す。画像を生成する場合は、result.presetのIDと版を指定したCreationRequestをlibrary.accept/executeへ渡す。HTTPはcharacters/saveとstories/saveで本文だけを保存する。

Client.availability.check(AvailabilityRequest(...))は必要な接続の観測を返す。kindはconversation、creation、image、collection、resumeから選び、対象IDまたは生成要求を渡す。確認は推論・モデル読み込み・GPU確保を行わない。

ConversationInputのuser・character・scenarioは、公開するPersonDefinition・CharacterDefinition・StoryDefinitionを使う。initial_stateはConversationStateを使い、自分と相手の服装・持ち物を別に保持する。文脈予算を超える場合だけ古い履歴の原文を引用して記憶を保存し、元の本文と現在状態は維持する。

開始時のplace・goalをinitial_stateへ指定しなければ、scenarioの同じ項目を使う。initial_stateへ明示した値は空文字列も含めて優先する。解決した初期状態は会話に保存される。現在状態の変更には行動を使い、開始状態への全置換は提供しない。プリセット経由とHTTPも同じ処理を使う。

旧状態を安全に変換できない会話はarchive.conversationsとarchive.conversationで読む。元の記録を新しい会話として推測変換しない。

画像の用途を直接指定する場合は `client.images.submit(..., purpose="banner")` または `generate()` を使う。用途の型は公開APIの `ImagePurpose` とする。用途別の生成条件は生成セットで管理し、未指定の単発画像には追加しない。再開は保存済みのRunを回収し、条件を再適用しない。

## 保存済み画像の参照を復旧する

アプリを閉じた状態で候補を確認し、確認IDを指定して適用する。候補確認はDBを変更しない。条件、保留理由、バックアップと再実行は[画像参照の復旧](../current/image-recovery.md)を参照する。

```python
inspection = client.inspect_image_links()
result = client.repair_image_links(inspection.id)
```

## 発言と行動を一緒に送る

```python
from komaquill.api import ConversationInput, MoveAction, SendInput

conversation = client.conversations.create(
    ConversationInput(title="A walk", llm_profile="lmstudio-4b")
)
saved = client.conversations.get(conversation.id)
operation = client.conversations.send(
    saved.id,
    SendInput(
        operation_id=uuid4(),
        expected_revision=saved.revision,
        text="I head to the beach.",
        action=MoveAction(destination="Beach"),
        locale="en",
        allow_real_llm=True,
    ),
)
receipt = client.operations.get(operation.id)
accepted = client.operations.input(operation.id)
```

この例では設定済みの実モデルを使う新しい会話を作成する。実行前に接続先を準備し、llm_profileを使用するプロファイルへ変更する。冒頭のfake会話は、対応する固定入力以外には使用しない。

発言と行動を一件として受け付け、返答より先に保存する。返答の失敗と行動の確定を区別し、同じ行動をやり直さず同じ操作IDで結果を確認する。operations.inputは保存済みの発言と任意の行動を読み、元の要求を変更しない。保存済みの行動はconversations.actions、会話中の案はConversation.proposalsで取得する。古い版の案は適用しない。詳細は[自分の行動と現在の場面](../current/player-actions.md)を参照する。
