# 標準機能への委譲と残る検証

状態: 設定、共通LLM実行、API生成、文書解析、画面の機械検査は実装済み。実LLMで限定比較を行ったが、REQ-LLM-EFFICIENCYの品質維持と総量削減の受入は未完了。

## 実装済みの範囲

[現在の設計](../current/library-delegation.md)に責務と検証方法をまとめる。要件ごとの状態と受入条件は[要件台帳](../requirements/catalog.toml)、採用理由は[ADR 0015](../decisions/0015-library-delegation.md)を正本とする。

- Pydantic Settingsの公開sourceによる合成と、用途別の検証済み設定の共有。
- 共通のLLM実行と会話・下書き・英訳・要約の分離、送信前の文脈予算、出典IDと使用量の集計。
- Hey APIによるQuery用ヘルパーの生成と、操作監視・終端保持の共有。
- markdown-it-pyによる文書解析と、日英のPlaywright・axe検査。

手動の再帰マージ、具象Responderへのcast、英訳側の再生成、旧LLM入口、旧query key、Markdown構文抽出の正規表現は置換した範囲で削除した。公開Python APIとHTTPの入力・出力は維持する。

## 残る受入

[LLM改善計画](langgraph-and-context.md)と[統合・復旧の検証](../verification/results/finish-library-and-llm.md)に、採用しなかった入力削減と残る生成品質を記録する。JSON圧縮で使用量が減った比較を、そのまま品質維持と効率化の達成にはしない。

日常検査はFakeと擬似APIを使う。新しい実モデルの比較は、既存証跡で判断できない仮説と代表条件がある場合だけ、許可と上限を固定して実施する。既存DBの画像参照は、独立した保全・適用作業で修復済みである。

Grimp、React Hook Form、Hypothesis、Lingua、永続グラフ、意味検索、追加キャッシュ、進捗配信は追加しない。
