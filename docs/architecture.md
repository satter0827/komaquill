# 構成と設定

## 処理の流れ

CLI → HTTPS → Cloudflare Quick Tunnel → Caddyの認証 → ComfyUIの標準API → GPU推論、の順に実行します。
ComfyUIとCaddyはColabのループバックで待ち受けます。HTTPとWebSocketの認証確認が成功してから接続先を表示します。

CLIはTyper、入力と保存データはPydantic、通信はHTTPX、モデル取得はhuggingface_hubとhf-xet、
資格情報はkeyring、回収の排他制御はfilelock、画像検証はPillowを使用します。

CLIから生成処理へ依存し、生成処理が通信・保存・ワークフローを使用します。
通信・保存からCLIへは依存しません。設定や型のインポート時に外部I/Oは行いません。
Colab専用のGPU依存はcolab追加依存に分離し、ローカルには導入しません。

## ワークフロー

配布用の画面JSONを編集し、ComfyUI標準のAPI形式エクスポートを更新します。
両形式の接続と値が一致することをテストします。初期ワークフローの元は
[ComfyUI公式のApp Mode試験用ワークフロー](https://github.com/Comfy-Org/ComfyUI_frontend/blob/v1.55.18/browser_tests/assets/linear-basic-app-1.json) です。

起動時に画面の既定表示だけを変えたテンプレートを作ります。ComfyUI標準のテンプレート配信に登録する小さなモジュールを配置します。
計算ノードやフロントエンド画面は追加しません。

## 保存と回収

送信前に要求とローカル実行IDを保存し、ComfyUIのextra_dataにも同じIDを渡します。
応答が失われた場合は履歴とキューを照合します。受付不明の要求は再送しません。
UIで作られた結果はランタイムIDと生成IDから保存先IDを作り、CLIの再起動後も同じ結果を二重保存しません。

回収は排他ロックの内側で行います。画像と条件を保存した後に状態を更新し、途中で失敗した場合はcollectで再開します。
ランタイムIDを照合するため、同じ接続先でComfyUIが入れ替わった場合も履歴を混用しません。

## 設定

`komaquill.toml` のpoll_interval、request_timeout、generation_timeoutで確認間隔と待機時間を変更します。
対応する `KOMAQUILL_POLL_INTERVAL` 等の環境変数が優先されます。
モデルの取得条件とColabの固定バージョンはパッケージのresources配下に置きます。

実行環境、ツール、接続情報はartifacts/runtime、モデルはartifacts/models、再作成可能なキャッシュはartifacts/cache、
作品と履歴はartifacts/runsに置きます。作品をまとめて削除する機能は用意していません。
