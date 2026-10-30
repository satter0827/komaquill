# 会話とローカルWebアプリケーション

React画面、HTTP API、CLIは公開Python APIの`Client`を使います。製品コードは`src/`に集約し、`frontend`、`backend`、`contracts`を独立した検証単位として管理します。

## 構成

```text
src/
  frontend/                 React・TypeScript・Vite
    src/app/                起動、画面枠、文言、スタイル
    src/features/           会話、単発画像、実行環境
    src/components/         Base UIを使う共有部品
    src/api/generated/      OpenAPIから生成する型とクライアント
    tests/                  Vitest・Testing Library・MSW
    package.json
  backend/
    src/komaquill/
      api/                  公開する機能別サービス
      conversations/        会話の型、状態更新、処理の順序
      images/               画像の送信、回収、完了待ち
      runtime/              起動、所有、監視、終了
      adapters/             DB、ファイル、LLM、ComfyUI、Colab
      archive/              旧記録を読む型
      cli/                  端末の入力と表示
      interfaces/http/      FastAPIのHTTP境界
      worker/               GPU側の準備と起動
      resources/            設定、文言、モデル、生成セット
    pyproject.toml
    uv.lock
  contracts/openapi.json
tests/                      Pythonの単体・結合・構造・E2E・実機検査
scripts/                    検証、契約出力、配布
bootstrap/                  環境の導入と更新
.artifacts/                 実行時データ、環境、ビルド、検証記録
```

Pythonの依存は[backendのpyproject.toml](../../src/backend/pyproject.toml)、開発時の共通検査は[ルートのpyproject.toml](../../pyproject.toml)、JavaScriptの依存は[pnpm-lock.yaml](../../pnpm-lock.yaml)が正本です。Pythonのテストは既存の分類と共通fixtureを維持し、ルートの`tests/`で管理します。

## 保存と処理

新しい会話、操作、画像要求、評価は作業フォルダーの`.artifacts/data/komaquill.sqlite3`へ保存します。SQLAlchemyと同梱したAlembicの移行を使用します。移行はプロセスとスレッド間で直列化し、SQLiteのDDLもトランザクションで確定します。入力素材、画像、ワークフロー、LLMの入出力はファイルへ保存します。GPU所有、監視、接続、停止の記録は独立した状態ファイルに残します。

会話送信は操作IDと入力ハッシュを保存して受け付けます。同一ID・同一入力は既存結果を返し、異なる入力は拒否します。作業フォルダーで同時に実行できる会話要求は一つです。LLM呼び出しの間はDBの書込みトランザクションを保持しません。

検証済みの応答と受付時の状態の版をDBへ保存し、本文と状態を一つのトランザクションで確定します。確定前にプロセスが失われた場合は保存済み応答から復旧し、外部モデルへ再送しません。古い版への応答は確定しません。その後で画像を送信し、本文の保存結果と画像の成否を分けます。処理中にプロセスが失われた場合はPIDと開始時刻を照合し、受付不明として保持します。実モデルへの要求を自動再送しません。

実モデルの応答は一回の構造化呼び出しで取得し、検証に失敗した場合だけ設定された回数まで修正します。固定定義・現在状態・今回の入力を保持し、文字数の入力上限に収まるよう古い履歴を省きます。固定部分だけで上限を超える場合は送信前に拒否します。SDKの自動再試行と外部トレースは無効です。実モデルへの送信には要求ごとの許可が必要です。

サンプルプロファイルは入力`Hello`と固定応答の組です。任意の入力を理解するモデルとして扱わず、実モデルへ切り替えません。

## HTTPと配布

APIは`/api/v1`に置きます。同じサーバーからビルド済みReactを配信し、ループバックへだけ待ち受けます。OriginとHostを検証し、APIの404をHTMLへ変換しません。本文の逐次配信は行わず、保存済み操作をポーリングします。

操作の保存形式はエラーコードを持ち、HTTPの表示用結果へ安全な説明を付けます。例外本文や外部APIの応答本文を画面へ渡しません。CLIの失敗記録には原因の型と処理段階を残します。

FastAPIとUvicornは`web`追加依存です。通常のPython APIのimportではサーバーやモデルSDKを読み込みません。配布工程は`.artifacts/`内でPythonソースとReactのビルド結果を組み合わせ、wheelとsdistへ同梱します。利用者の環境にNode.jsは不要です。

旧Storyの新規生成入口は廃止しました。`client.archive`は旧記録の一覧、取得、書き出しを読み取り専用で扱います。元の作品、履歴、画像、評価を自動移行しません。

画像生成とGPU管理の既存の契約は[現在の構成](architecture.md)と[プリセット](presets.md)を参照してください。設計上の判断と残る確認項目は[統合計画](../plans/roleplay-chat.md)で管理します。
