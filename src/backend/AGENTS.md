# Python製品の開発

- 公開入口はkomaquill.apiとする。CLIとHTTPは同じ機能別サービスを呼ぶ。
- conversations、images、domain、applicationは具体的な保存、SDK、表示へ依存しない。
- 接続の確保、画像生成、会話の保存を分離する。workerへDBとLLM設定を配布しない。
- Python依存はこのフォルダーのpyproject.tomlとuv.lockを正本とする。
- 検証はリポジトリルートのpnpm check、テストはルートのtestsで管理する。
