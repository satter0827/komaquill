# 構成と設定

## 接続

WindowsのCLI → ループバックの共有SSH転送 → Colab上のComfyUI標準API → GPU推論、の順に実行します。
ComfyUIもGPU側のループバックで待ち受けます。SSHの通信は公式Colab CLIのWebSocket接続を通ります。
外部ComfyUIには標準APIで直接接続します。常駐サービスや独自のkeep-aliveは追加しません。

公式Colab CLIは0.7.5に固定し、Pythonのモジュールを介して呼びます。詳細ログと例外のローカル変数表示を抑制します。
公式の `--config` でプロジェクトごとのセッション状態を分離し、所有するランタイムIDを照合します。
SSHには未登録セッションの自動作成経路があるため、状態確認からSSH認証完了まで起動・接続・停止のロックを保持します。
ホスト鍵はランタイム単位で記録し、次の接続から一致を検証します。

## 役割

- `cli.py`：Typerによる操作と日本語メッセージ。
- `runtime.py`：接続の維持、自動回収、終了待ち。
- `colab.py`：公式CLIとOpenSSHによる確保・転送・接続・終了。
- `worker.py`、`scripts/worker.py`：GPU側の環境準備とComfyUIの起動。
- `service.py`：生成要求、履歴照合、画像の回収。
- `client.py`、`storage.py`：HTTP通信と原子的保存。
- `workflow.py`、`downloads.py`：配布ワークフローとモデルの取得。

生成・保存からColab制御へ依存しません。HTTPX、Pydantic、filelock、Pillow、huggingface_hub、keyringを使用します。
ローカルの公式CLI依存は `colab-client`、GPU依存は `worker` に分離しています。

## ソースとモデル

HEADのGitアーカイブを転送し、SHA256を照合してからランタイムへ展開します。転送物にGitの作業ツリー、資格情報、モデルは含めません。
ComfyUIのコミットとフロントエンドの版を固定し、uv.lockでGPU依存を固定します。実行時のパッケージ一覧も回収します。

SD 1.5 FP16のモデルはローカルとGPU側が配布元から直接取得し、同じSHA256を照合します。
Windowsからの大容量アップロードを省きます。既存ランタイムのモデルは再利用し、ランタイムを作り直した場合だけ再取得します。

画面とAPI用のJSONを配布し、接続と値の一致をテストします。入力画面とノード画面は標準テンプレートで切り替えます。
計算ノードや画面を独自実装しません。

## 状態と保存

送信前に要求を保存し、同じ実行IDをComfyUIのextra_dataにも渡します。受付不明のPOSTは再送せず、履歴とキューを照合します。
画像を検証して原子的に保存した後、保存済み状態を記録します。生成と自動回収の状態更新は同じロックで保護します。
画面の結果は接続の識別IDとComfyUIの生成IDから一意に保存します。

Colabは識別ファイルを照合します。外部ComfyUIに専用ファイルは要求せず、確認できないソース・モデル・環境情報はnullとして保存します。
外部接続では、接続前の履歴とキューを除外します。標準APIだけでは外部サーバーの入れ替わりを完全には識別できません。

## 設定と保存先

`komaquill.toml` と対応する `KOMAQUILL_項目名` の環境変数を使います。環境変数が優先されます。

| 項目 | 既定値 | 用途 |
|---|---:|---|
| gpu | T4 | 新規に確保するGPU |
| local_port | 18188 | Windows側の接続ポート |
| remote_port | 8188 | GPU側のComfyUIポート |
| poll_interval | 2秒 | 確認の間隔 |
| request_timeout | 30秒 | API通信 |
| preparation_timeout | 900秒 | 環境とComfyUIの準備 |
| generation_timeout | 600秒 | generateの待機 |
| shutdown_timeout | 600秒 | 終了前の生成待機 |
| control_timeout | 90秒 | 公式CLIとSSHの制御 |
| execution_timeout | 86400秒 | GPU側でComfyUIを実行する上限 |

`artifacts/` に画像、モデル、履歴、ログ、環境、SSH鍵を集約し、全体をGitから除外します。
Google認証、公式CLIのログ・履歴・セッション状態は `~/.config/colab-cli/` に保存します。OAuthの実装を独自に持ちません。
外部ComfyUIの任意のBasic認証は、利用者名を接続情報に、パスワードをOSの資格情報ストアに保存します。
