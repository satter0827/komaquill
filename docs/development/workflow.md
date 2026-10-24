# 開発とリリース

## 環境を準備する

[CONTRIBUTING](../../CONTRIBUTING.md#環境を構築する)に従って導入します。bootstrapは環境の導入・有効化・依存更新、scriptsは品質検査、testsはテストと検証コードを担当します。[開発規約](../../AGENTS.md)を正本とします。

## 品質検査を実行する

作業フォルダーのルートから実行します。Check Projectも同じ入口を使用します。

| レベル | 対象 |
|---|---|
| L1 | 整形、lint、strict mypy、依存方向、文書、作業ツリーの秘密情報 |
| L2 | L1に加え、各言語のテスト、GPU依存ロック、カバレッジ、実プロセスでの基本操作 |
| L3 | L2に加え、隔離ビルド、配布物、隔離wheelの基本操作、Git履歴の秘密情報 |

```powershell
python scripts/check.py --level 2
python scripts/check.py --level 3
python scripts/check.py --level 3 --plan --json
python scripts/check.py --level 3 --status --json
```

既定はL2です。上位レベルは下位の全検査を含みます。GPU、Google認証、有料APIは要求しません。依存導入には配布元への通信が必要です。生成操作の検査はlocalhostの代替サーバーを使用します。

実行計画には検査ID、必要環境、コマンド、前提となる検査を含めます。個別調査では--check installed-wheelのように指定できます。前提工程も実行しますが、レベル全体の成功としては登録しません。

## 結果を確認する

結果は.artifacts/records/reports/quality/実行IDに保存します。gate.jsonには対象コミット、入力ハッシュ、工程の時刻、終了コード、失敗種別、未実行理由を記録します。実行管理と結果保存は製品モジュールをimportしません。

latest-attempt-lN.jsonは最新実行、latest-success-lN.jsonは最新の全工程成功です。statusのmatches_current_inputsで現在と同じ入力を検査した結果か確認します。古い成功は現在の成功を意味しません。

失敗工程に依存する検査は省略し、独立した秘密情報検査は続けます。中断、入力変更、部分実行では最新成功を更新しません。WindowsとLinuxのCIもL3を実行し、qualityとsecurityを維持します。

カバレッジは製品・検査・環境更新のPythonを、未実行ファイルも含めて計測します。testsは除外します。対象と行・分岐の閾値は[pyproject.toml](../../pyproject.toml)を正本とします。PowerShellは構文・引数・終了コード・出力先をWindowsで別途検査します。

## 依存を更新する

pyproject.tomlとuv.lockを更新した後、GPU用の固定依存を明示的に書き出します。

```powershell
python -m bootstrap.update_worker_lock
python scripts/check.py --level 3
```

品質検査は追跡対象ファイルを書き換えません。GPU用依存はworkerグループから出力し、ローカルへ導入しません。公開ハッシュの検査除外には理由を残します。

Pythonの版は.python-versionとGPUの固定定義を揃えます。ComfyUIの更新時は同じ版のrequirements.txtをtests/fixturesへ保存し、ロックとの対応を確認します。

## 公開する

PRで変更と関連検証をまとめます。接続・保存・GPU転送の変更は[検証戦略](../verification/strategy.md)に従って実機でも確認します。

バージョンの正本はpyproject.tomlです。SemVerを基本とし、事前版はPEP 440を使います。必須チェックが成功したmainから宣言版と一致するタグを作成します。公開済みタグの変更とPyPIへの自動公開は行いません。
