# 画面の開発

- 画面はHTTP APIを使う。DBやローカルの任意パスを扱わない。
- サーバー状態はTanStack Query、画面内の入力はReactで管理する。更新要求を自動再送しない。
- 表示文言はsrc/app/locales/ja.jsonとen.jsonへ置く。日本語を既定とし、保存済みの本文と画像だけを表示する。
- src/api/generatedはルートのpnpm contractsで更新する。
- appは機能を組み立てる。featuresから別機能のページとappのコードへ依存しない。共通処理はsharedへ置く。sharedからappへの参照は表示文言のja.json・en.jsonの読み取りだけを認め、ESLintで検査する。
- 検証はリポジトリルートのpnpm checkで変更範囲を選択する。
