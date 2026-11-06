# アプリケーションの構成

## 処理の入口

ReactはHTTPを呼び、HTTPとCLIは`komaquill.api.Client`の機能別サービスを呼びます。公開Python APIは表示と終了コードを決めません。import時に通信、ファイル作成、認証、GPU確保を行いません。

## ソースの配置

```text
src/
  backend/src/komaquill/
    api/                    公開Python API
    composition/            具体的な実装の組み立て
    interfaces/cli/         CLIの入出力
    interfaces/http/        HTTPの入力と応答
    interfaces/comfyui/     ComfyUI標準画面の拡張
    application/<機能>/     操作の調整
    domain/<機能>/          状態、判断、保存・入力契約
    adapters/persistence/   SQLiteの接続、テーブル、機能別リポジトリ
    adapters/llm/           推論、トークン計測、文脈、再開
    adapters/compute/       GPUと接続の制御
    worker/                 GPU側の導入と起動
    resources/              設定、文言、モデルとプリセット
  frontend/src/
    app/                    起動、経路、画面の組み立て
    features/<機能>/        機能別の画面と操作
    shared/                 共通UI、言語、状態の購読
    api/generated/          OpenAPIから生成した通信契約
  contracts/                OpenAPIの正本
```

`domain`と`application`は表示、DB、通信、ファイル、GPU制御をimportしません。交換が必要な境界には既存のProtocolや型付きの関数を使い、将来のためだけに汎用サービス層を増やしません。Reactの機能は他機能のページや`app`へ依存せず、共通の画像表示と接続購読は`shared`へ置きます。

Reactの文言の正本は`app/locales`です。`shared/messages`はこのJSONを読み取ります。このデータ参照だけをESLintの依存制約から除き、`app`の実行コードへの依存は許可しません。機能間の組み立ては`app`で行います。

`tools/`は決定的な検査と開発の実行管理、`scripts/`は実接続・評価・探索、`bootstrap/`は環境導入、`tests/`は決定的な回帰検証を担当します。製品からこれらへ逆依存しません。実行管理は製品を同じプロセスへimportせず、子プロセスの終了と証跡を扱います。

## 設定と実行

CLIの指定、環境変数、作業設定、同梱の既定値の順に解決します。接続と制限は起動時に確定し、回収と終了でも共有します。保存済みの試行を再開する場合は元の条件を使用します。正本は[Pythonの設定](../../src/backend/src/komaquill/resources/settings/)と[LLMの設定](../../src/backend/src/komaquill/resources/conversations/defaults.toml)です。

## 保存、接続、表示

保存の正本と再構築可能な出力は[保存設計](storage.md)を参照してください。画面の更新や集計の失敗で保存済み状態を後退させません。受付が不明な送信を自動再送しません。

画像生成はComfyUI標準API、実行状態の再開は公式LangGraph SQLite saver、DBはSQLAlchemyとAlembic、表示は既存のReact・TanStack Query・Base UIを使います。独自ノード、認証基盤、汎用プラグイン層は追加しません。家庭内LANは[接続の保護](security.md)に従います。

GPU確保は明示的なstartだけで行い、再接続から確保しません。所有ID、アカウント、終了確認を照合し、外部サーバーを停止しません。GPU転送物は[provenance.py](../../src/backend/src/komaquill/adapters/provenance.py)の宣言と配布検査で確認します。


GPUの起動に必要なuvは、確保前にローカルで公式配布物と固定SHA256を照合し、転送物へ含めます。GPU側からuvの配布先へ再取得しません。転送物とツールの内容をそれぞれ照合してから実行し、環境準備の取得失敗で不要なGPUを確保しないようにします。
