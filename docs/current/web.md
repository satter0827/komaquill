# React画面とHTTP API

対象: REQ-CHAT-LIBRARY、REQ-REACT-WEB、REQ-PLAY-COMPOSITION。

Reactの画面はFastAPIの `/api/v1` を呼び、CLIと同じ公開Python APIへ接続する。Pythonが保存、LLM、画像、実行状態を管理する。画面を閉じても所有GPUは自動で終了しない。

画面の構成、設定の固定、画像処理、二言語の契約は [個人設定とプリセット](creators.md) を参照する。会話は保存済みの自分・相手・ストーリーから開始する。既存の会話と画像、標準のApp ModeとNode Graph、単発画像生成を利用できる。

OpenAPIは [API契約](../../src/contracts/openapi.json)、TypeScriptの生成先は `src/frontend/src/api/generated`。識別子と保存形式は業務契約に置き、HTTPの失敗は安全なコードを返す。Reactはコードに対応する表示言語の説明を選ぶ。

Viteの開発画面はAPIへプロキシ接続する。配布版はビルド済みReactをwheelへ含め、FastAPIから配信する。配布版の実行にNode.jsは不要。生成・起動・検査の操作は [画面の使い方](../guides/web.md)、検査対象の選び方は [開発手順](../development/workflow.md) を参照する。
