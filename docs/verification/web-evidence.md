# 画面のE2Eとレビュー用証跡

保存、選択、送信、再表示など、期待結果が一意に決まる検査は`tests/system/e2e/web/`へ置きます。分かりやすさ、文章の自然さ、画像の魅力など、判断が必要な観察は`scripts/review/web/`へ置きます。証跡の収集処理は`tools/testing/web/`で共有します。要件はREQ-WEB-EVIDENCEです。

## 実行する

Python環境とNode依存を[開発手順](../development/workflow.md)に従って導入し、PlaywrightのChromiumを準備します。

```powershell
node tools/testing/playwright.mjs install chromium
pnpm test:web
pnpm review:web
```

`test:web`はビルド済みの画面と実際のHTTP APIをブラウザーで操作し、値、保存後の再表示、画像の取得、言語切替、狭い画面、キーボード操作、エラーからの復旧を検査します。キャラクター、ストーリー、自分のプロフィールを作成し、キャラクターの編集・複製・削除と、削除後の会話・画像の保持も確認します。

`review:web`は同じ画面で一連の操作を実行し、評価用の材料を保存します。実行完了は`captured`であり、使いやすさや作品品質の合格ではありません。

通常の品質ゲートは`tests/`の検査を実行します。収集処理も、`tests/tools/integration/web/evidence.test.mjs`で秘匿化、ハッシュ照合、欠落、失敗、状態分類を検査し、`tests/system/e2e/web/evidence.spec.ts`で実ブラウザーからの収集を検査します。主観評価のシナリオは通常のテスト検出へ混ぜません。JavaScriptの開発用検査はNxの`tools:test`が担当します。

## 接続先と保存範囲

実行ごとに新しい証跡フォルダーを`.artifacts/records/reports/web/`へ作成します。各ケースは別の作業フォルダーを持ち、配布wheelのCLIをloopbackのOS割当ポートで起動します。既存の作品、個人設定、起動済みの画面は使用しません。

LLMとComfyUIは、ローカルの検査専用APIに置き換えます。LLMは決められた検査用応答、画像は小さな検査用PNGです。実モデルへの許可と接続用環境変数を引き継がず、GPUは確保しません。実LLMの応答品質、日本語の英訳品質、生成画像の画質や人物の一貫性は、この結果では判断できません。

操作ごとに画面・本文・入力値を保存します。Playwrightのtraceにも通信内容が含まれるため、この実行入口は隔離した架空データ専用です。実ユーザーの保存先や接続認証を持つ画面へ転用しません。イベント一覧には認証ヘッダー、応答本文、例外本文、URLのクエリーを保存しません。

## 証跡を読む

```text
.artifacts/records/reports/web/<実行ID>/
├── run.json                 # 対象コミット、入力ハッシュ、環境、実行範囲
├── index.json               # ケース別結果、証跡の整合性、参照先
├── fixture.json             # 隔離した設定と検査用接続の識別
├── review-questions.json    # AIが評価する観点と所見の項目
├── scenarios/<ケースID>/
│   ├── scenario.json        # 操作順、時刻、結果、ファイルのSHA256
│   ├── 001.png              # 操作後のスクリーンショット
│   ├── 001.aria.txt         # アクセシビリティ構造
│   ├── 001.dom.json         # 見出し、入力状態、画像寸法、画面幅
│   └── events.jsonl         # 時系列の通信結果・ブラウザー診断
├── playwright/              # Playwrightのtraceと失敗時の詳細
├── html/index.html          # 人が確認するレポート
└── workspace/               # 今回だけの保存先
```

AIは最初に`run.json`で対象版・実接続の有無・入力変更の有無を確認し、`index.json`から対象ケースを選びます。次に`scenario.json`の操作順と画像・DOM・アクセシビリティ構造を照合します。各操作の`event_sequence`は、その撮影直前までのイベント番号です。通信は要求IDと接続元のハッシュで対応付け、公開APIの固定経路を残します。利用者のID・ファイル名・クエリーは伏せているため、通信内容の推測には使いません。

各ファイルのハッシュとサイズ、各操作の3種類の証跡を検査します。欠落・改変・未実行を成功にしません。失敗時も最後の画面と、それまでの操作を保持します。`--list`は検出結果であり、画面操作の成功には数えません。

所見には観点ID、操作ID、証跡の相対パス、観測した事実、影響、改善案、確信度を付けます。スクリーンショットで分かる事実と、原因の推測を分けます。8ピクセルの検査用画像から画質を評価しません。

## 結果の区別

| outcome | 意味 |
|---|---|
| `passed` | 明確な期待結果を確認した |
| `known_unmet` | レビューで指定された期待結果が現在の実装では未達 |
| `unexpected_pass` | 未達として登録した期待結果を満たした。既知未達の登録を見直す |
| `captured` | 観察材料を取得した。主観評価は未実施 |
| `failed` | 通常の検査または収集に失敗した |
| `not_executed` / `interrupted` | 未実行または中断した |

「会話はモデルを既定」「メッセージごとの切替不要」「自分は1人」「年齢・性別」は、`review-requirements.spec.ts`の通常の受入テストとして実行します。スキップはしません。Playwrightの集計では期待失敗も実行成功に含まれるため、要件達成の判断には必ず`index.json`の`known_unmet`を確認します。現在の受入テストには期待失敗の指定はありません。

検査用APIの成功、画像の表示、実モデルの品質、ゲームとしての楽しさは別の判断です。以前の成功を現在の版の受入結果として扱いません。

## 実機の受入

`pnpm verify:live --allow-real-llm --output .artifacts/records/reports/live/attempt-1`で実LLMを確認します。`--allow-gpu`を加えると隔離した作業場所でT4の生成と標準画面まで確認します。今回所有したGPUは終了時に解放し、既存の利用者の作品や起動済み画面は使いません。上限と再確認の手順は[検証方針](strategy.md)を参照してください。

標準ComfyUIの未作成の任意ファイルは、固定版の[APIの空結果への変換](https://github.com/Comfy-Org/ComfyUI_frontend/blob/v1.55.18/src/scripts/api.ts)、[テンプレート読取](https://github.com/Comfy-Org/ComfyUI_frontend/blob/v1.55.18/src/extensions/core/nodeTemplates.ts)、[追加スタイルの読込](https://github.com/Comfy-Org/ComfyUI_frontend/blob/v1.55.18/vite.config.mts)に従い、画面起動中だけ接続元・経路・GET・404・最大件数を指定して判定します。他のHTTP失敗やアプリケーションのconsole.errorは除外しません。成功した空本文の応答を受信した後の本文キャンセルは、受付前の送信中断と分けて記録します。本文のある応答や受付を確認できないPOSTの中断は失敗です。

会話画像は自動生成せず、「今の場面を描く」で受付時の現在状態を描く。キャラクターとストーリーの保存後は代表画像とバナーを作成する。

## 実4BとWAIの確認

日常のテストと実機の収集を分ける。次の操作は実4Bを使い、日本語と英語それぞれで履歴枠を超える会話を保存する。

```powershell
node tools/runtime/python.mjs -m scripts.evaluation.images.roleplay --allow-real-llm --phase conversations
```

画像の確認は、上記で出力されたworkspaceを明示指定する。T4を起動し、WAIで代表画像・バナー・会話画像・単発画像を生成して終了する。上限は[実機確認の設定](../../scripts/evaluation/images/budget.toml)を使う。

```powershell
node tools/runtime/python.mjs -m scripts.evaluation.images.roleplay --allow-real-llm --allow-gpu --phase images --workspace <workspace>
```

収集結果のfunctional_statusは保存・処理の成否であり、review_statusは別に扱う。台詞、場面説明、状態、要約、英訳、PNGを実際に照合し、評価は元のevidence.jsonを変更せず別の記録へ保存する。使用モデル・対象版・入力ハッシュ・使用量・時間・GPU終了確認と、期待との差を残す。
