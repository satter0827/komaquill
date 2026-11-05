# 視覚設計と共通操作の委譲

状態: 採用。対象: REQ-WEB-VISUAL-IDENTITY、REQ-CONVERSATION-ALBUM-UX。

## 背景

表示の配色・文字サイズと、操作の開閉・配置・フォーカスが画面ごとに定義されていた。装飾だけを加えると、余白の競合や小画面での操作漏れを残す。既存のBase UIはボタンに限られていた。

## 判断

既存のBase UI DialogとPopoverへ共通操作を委譲する。名前や行動を入力する小窓にはPopoverを使い、選択専用のMenuへ入力フォームを持ち込まない。既存の標準dialogを継続するという[以前の判断](0015-library-delegation.md)のうち、ReactのモーダルをBase UIへ統一する部分を更新する。

アルバムの日付と詳細は標準details、入力と選択は標準HTMLを維持する。末尾追従はuse-stick-to-bottomを使用し、独自の位置判定やサイズ監視を追加しない。依存の版は依存定義とロックを正本とする。

共通部品は見た目と既定値を揃える範囲とし、ライブラリーの公開APIを一括して包まない。配色・文字・間隔・操作状態はCSS変数で共有する。新しいフォーム・アニメーション基盤や画像ビューアーは追加しない。

画面の非表示もDialogを使い、ポータルを含めて完全に覆う。表示内容の高さとマウント状態を変更せず、下書きと閲覧位置を維持する。復帰ボタン以外では解除しない。

## 制約と検証

公開API、保存済み本文、画像、実行条件、監視と回収の責務は変更しない。小窓の入力状態と受付済み要求を表示部品の寿命から分離する。見た目の合格、操作の合格、実モデルの応答品質を分けて記録する。

## 参考資料

- [Base UI Dialog](https://base-ui.com/react/components/dialog)
- [Base UI Popover](https://base-ui.com/react/components/popover)
- [use-stick-to-bottom](https://github.com/stackblitz-labs/use-stick-to-bottom)
