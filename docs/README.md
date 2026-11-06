# 文書の索引

利用者は操作手順、開発者は構成と検証方針から確認してください。現在の設計、未完了の計画、特定版の検証結果を分けています。

| 目的 | 文書 |
|---|---|
| 導入と基本操作 | [利用手順](guides/usage.md)、[会話画面](guides/web.md)、[Python API](guides/python-api.md) |
| 要件と受入条件 | [対象範囲](product/scope.md)、[要件台帳](requirements/catalog.toml) |
| 構成と依存方向 | [現在の構成](current/architecture.md)、[開発手順](development/workflow.md) |
| 保存とDB | [保存設計](current/storage.md)、[生成物の配置](operations/artifacts.md)、[生成物の整理](operations/storage-cleanup.md)、[バックアップ](operations/backup.md) |
| セキュリティとLAN | [接続の保護](current/security.md) |
| モデルの実行・来歴・評価 | [モデル運用](current/model-operations.md) |
| 機能別の設計 | [会話と画像](current/web.md)、[操作と接続確認](current/operations-and-availability.md)、[状態と記憶](current/execution-and-content.md) |
| テストの範囲と実行 | [検証方針](verification/strategy.md)、[検証資料](verification/README.md) |
| 再設計の確認結果 | [基本動作・保存・検証基盤](verification/results/reliability-redesign.md)、[保存と障害境界の再レビュー](verification/results/reliability-review.md)、[会話の継続と表示](verification/results/conversation-continuity.md)、[T4標準画面の最終確認](verification/results/standard-ui-final.md) |
| 容量と削除候補 | [生成物・ログ・ワークツリーの整理前調査](verification/results/storage-review-20261106.md) |
| 設計判断 | [再設計の判断](decisions/0022-reliability-and-storage.md) |
| 未完了の作業 | [今後の対象](plans/future.md) |

`current/`は現在の実装、`plans/`は未完了の作業、`verification/results/`は対象版と環境を限定した検証結果です。過去の成功は、現在のコードや接続先の成功を示しません。設定値・依存版・エラー文言はコードと設定ファイルを正本とします。
