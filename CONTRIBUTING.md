# 開発手順

PowerShell 7で `./scripts/setup.ps1` を実行し、`. ./scripts/activate.ps1` で環境を有効にします。

変更は `feat/内容`、`fix/内容`、`docs/内容` などの短い作業ブランチで行います。
コミットは `feat: 画像回収を追加` のように目的が分かる日本語で記載します。
mainへのマージにはPRとCIの成功が必要です。マージはリポジトリ所有者が行います。

```powershell
python scripts/check.py
python scripts/security.py
```

整形、import整理、lintはRuff、型検査はstrict mypy、テストはpytest、依存構造はImport Linterに集約します。
docstringはGoogle形式を基本とします。1つのPRで解決する目的を明確にします。
既存規約の参照元は [python-app-template](https://github.com/satter0827/python-app-template) です。

詳細は [開発とリリース](docs/development.md) を参照してください。
