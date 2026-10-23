# 開発とリリース

## 構成

src/は製品、bootstrap/は初回導入、scripts/は品質ゲート、tests/はテストと検証コードを担当します。文言・保存・依存方向などの規約は[AGENTS](../AGENTS.md)を正本とします。

## 品質検査

[環境構築](../CONTRIBUTING.md#環境を構築する)後、python scripts/check.pyとpython scripts/security.pyを実行します。Check Projectも同じ処理を呼び出します。

check.pyはRuffの整形確認・lint、strict mypy、GPU依存ロックの一致、Import Linter、pytest、行・分岐カバレッジを順に確認します。srcとscriptsの未実行Pythonファイルも対象とし、testsは除外します。閾値はpyproject.tomlのtool.komaquill.coverageで管理します。

security.pyは公開対象、ステージ済み差分、全Git履歴をdetect-secretsで検査します。検出値を外部送信する確認機能は無効です。レポートへはファイル、行番号、検出種類だけを保存します。公開ハッシュや試験値の除外には行単位で理由を記載します。setup.ps1は同じ検査を送信前フックへ登録します。

品質結果は.artifacts/reports/quality/へ保存します。自動テストは模擬応答と一時フォルダーを使い、GPUやGoogle認証を要求しません。PowerShellの構文・パス・終了コードはWindowsで確認します。

## 依存を更新する

pyproject.tomlとuv.lockを同時に更新します。GPU用の依存をローカル環境へ導入しないでください。ロック変更後は次を実行します。

```powershell
python scripts/check_worker_lock.py --write
python scripts/check.py
```

公開配布物のURL・バージョン・ハッシュを保存し、GPUではハッシュ検証付きで導入します。書き出し時は公開SHA256の行だけに検査除外の理由を付け、ファイル全体は秘密情報検査の対象に残します。Pythonの版を変更する場合は.python-versionとresources/runtime.tomlを揃えます。公式CLIやComfyUI、接続・保存・終了を変更した場合は[実機検証](validation.md)も行います。

## 配布物を確認する

```powershell
uv build --out-dir .artifacts/dist
python scripts/check_package.py
$env:KOMAQUILL_WHEEL = '.artifacts/dist'
python -m pytest tests/test_wheel.py -q
```

sdistとwheelのバージョン、ワークフロー、設定・起動用ファイルの同梱、生成物と資格情報の除外を確認します。隔離環境ではGitと編集可能インストールに依存せず、init、外部接続、生成・保存、終了、GPU用転送物作成を確認します。WindowsとLinuxのCIも同じ検証を実行します。

## バージョンとリリース

バージョンの正本はpyproject.tomlです。SemVerを基本とし、Pythonの事前版にはPEP 440形式を使います。uv version --bump patchなどでロックと一緒に更新します。0.xの破壊的変更ではminorを上げます。

qualityとsecurityが成功したmain上で、宣言した版と一致するv0.1.0などのタグを作成します。公開済みタグは変更・削除しません。リリースノートには利用者への変更と必要な操作を記載します。PyPIへの自動公開は行いません。
