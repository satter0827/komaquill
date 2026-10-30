# 開発への参加

## 環境を構築する

WindowsではPowerShellから`./bootstrap/setup.ps1`を実行します。Pythonは`.artifacts/runtime/venv/`に作成します。続いてルートで`pnpm install`を実行します。

```powershell
pnpm dev
pnpm check
```

## 変更する

[要件台帳](docs/requirements/catalog.toml)と[現在の構成](docs/current/web.md)を確認し、製品、テスト、要件、操作手順を一緒に更新します。文書・コミット・PRの説明は日本語、識別子・コード内の説明・製品の表示文言は英語とします。

関連する変更と検証を一つのPRにまとめます。保存済み作品、個人設定、秘密情報、実行中のGPU所有を保全します。不要な互換入口と重複する処理は残しません。

## 検証する

日常は`pnpm check`、PRは`pnpm check:ci`、公開前は`pnpm check:all`と`pnpm build`を実行します。検証範囲、キャッシュ、結果、配布の扱いは[開発とリリース](docs/development/workflow.md)を参照してください。GPUと実LLMは自動検証へ含めません。

## PRとリリース

mainへの変更はPRを使い、qualityとsecurityを維持します。コミットとPRは変更の目的、結果、検証を日本語で説明します。リポジトリ所有者がマージします。
