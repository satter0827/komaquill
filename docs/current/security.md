# 接続とデータの保護

## 既定の接続

アプリケーションはlocalhostだけで待ち受けます。許可していないHost、別Origin、cross-siteの要求と、上限を超える本文を拒否します。APIと私有画像には保存抑制の応答ヘッダーを付け、フレーム埋め込み、外部スクリプト、MIME推測を制限します。入力名と保存キーを物理パスとして使用せず、作業場所の外へ出る参照を拒否します。

画像は形式、寸法、内容のハッシュを検査します。外部の資格情報はOSの資格情報ストア、Google認証は公式CLIの管理先に保持します。例外本文・変数、HTTP本文、資格情報を診断へ転記しません。診断には安全なエラーコード、段階、操作ID、コード位置を残します。

## 家庭内LAN

ログインアカウントは作成しません。`komaquill.toml`の`[web]`に、利用するHTTPSの`origin`を一つ指定してください。制限の既定値は[web.toml](../../src/backend/src/komaquill/resources/settings/web.toml)で管理します。設定変更は再起動後に反映されます。

1. `komaquill web-proxy`でCaddyfileを作成します。
2. Caddyの標準機能でHTTPSを公開します。`tls internal`のCA証明書を利用端末へ信頼登録してください。APIとComfyUIの待ち受けをLANへ直接公開しません。
3. 端末でHTTPSのURLを開き、表示された端末IDを確認します。
4. 実行中のPCで`komaquill devices`を確認し、`komaquill approve-device ID`で該当端末を承認します。
5. 端末のページを再読み込みします。失効させる場合は`komaquill revoke-device ID`を使います。

端末CookieはSecure・HttpOnly・SameSite=Strictとし、DBにはトークンのハッシュだけを保存します。未承認・期限切れ・失効後はAPI、画像、画面資源を拒否します。端末の承認・失効はローカルの操作に限定します。ComfyUIとWebSocketもCaddyの`forward_auth`で同じ判定を通します。

未承認端末の登録件数を制限します。登録上限や端末記録の保存失敗も、APIと同じ安全なエラー形式で返し、応答にセキュリティヘッダーを付けます。失敗した登録でCookieを発行しません。

端末の登録・承認・失効は追記する履歴に残します。CaddyはOSの信頼ストアを自動変更しません。CAの信頼登録は利用端末で明示的に行ってください。Pythonはpip-audit、Nodeはpnpm auditで固定依存の既知の脆弱性を検査し、CIにJSONの結果を保持します。

Pythonは全extrasと全dependency groupsのハッシュ付きexportを監査します。CUDA版のようにPyPIにないビルド名も照合するため、[pip-auditの標準OSV接続](https://github.com/pypa/pip-audit)を使います。GPU依存や開発依存を除外せず、監査不能も失敗とします。OSVでの監査はハッシュの存在を要求し、配布物の実体のハッシュ照合はuvによる固定依存の導入時に行います。

WebSocketの接続時も承認を確認します。接続済みのストリームはweb.tomlの`websocket_seconds`で期限を設け、失効後に接続を保持し続けないようにします。認証照会ではUpgradeヘッダーを除き、許可後の転送で標準のWebSocket接続を確立します。

アプリケーションはloopbackのCaddyだけを信頼するプロキシとして扱います。Caddyはクライアントが渡した転送元情報を採用せず、接続元から転送情報を作成します。[Caddyの標準仕様](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)と[forward_auth](https://caddyserver.com/docs/caddyfile/directives/forward_auth)を参照してください。インターネット公開はこの構成の対象外です。

## 検証

Host・Origin・本文上限、端末承認の再起動後の保持、失効、管理操作、保存先とCookieの保護は[HTTPの回帰テスト](../../tests/backend/integration/http/)で確認します。秘密情報と配布物の検査も実行します。Caddyを介する実接続、CAの信頼登録、GPU上の標準画面は、実機の結果で別々に判定します。
