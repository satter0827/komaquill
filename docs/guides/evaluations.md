# 生成結果を評価する

## 分析用の記録を出力する

初期化済みの作業フォルダーで実行します。

```powershell
komaquill evaluations export --output .artifacts/records/reports/evidence.json
```

未評価の過去生成も対象です。生成条件、モデル・素材・画像の識別情報、評価記録、評価JSONのスキーマを出力します。モデル本体、画像のバイナリ、接続設定、認証情報、ログは含めません。既存の出力先は上書きしません。

artifact_baseは作業フォルダーからの基準位置、各keyはその配下の保存キーです。AIが画像を評価する場合は、呼び出し側がこの参照から画像を読み込みます。JSONだけでは画像の見た目を評価できません。外部送信、評価APIの呼び出し、GPU確保は自動で行いません。

--run-idで一件を指定できます。出力のrun_idを以下のRUN_IDへ指定します。

## 採否と理由を残す

```powershell
komaquill evaluations add RUN_ID --decision accept --criterion composition --reason "Clear foreground."
```

判定はaccept、reject、undecidedです。プリセットの変更や画像の削除は行いません。criterion-versionは評価基準の版で、既定は1です。基準を変更した評価は版を区別します。

--imageは対象画像の保存名、--case-idは同じ題材のID、--evaluatorは評価者です。終了した生成を対象とし、失敗・回収不能の生成についても条件や失敗を評価できます。

## 二つの生成を比較する

```powershell
komaquill evaluations compare RUN_A RUN_B --preference second --criterion composition --reason "Better framing."
```

選好はfirst、second、equal、undecidedです。採否とは別に保存します。同じ題材と基準の版で複数のseedを比較し、一枚だけで設定全体の優劣を決めないようにします。

## AIの評価を記録する

```powershell
komaquill evaluations add RUN_ID --decision accept --criterion composition --reason "Clear foreground." --evaluator reviewer --ai-model MODEL_ID --model-version MODEL_VERSION --instructions instructions.txt
```

指示ファイルはUTF-8で読み、内容とSHA256を保存します。確認できないモデル版は省略し、推測で埋めません。AIにも根拠と判断保留を残します。

exportのevaluation_schemaを使ってAIから構造化した評価JSONを受け取り、登録できます。

```powershell
komaquill evaluations import reviews.json
komaquill evaluations list --run-id RUN_ID
```

importは評価の配列、またはformat_versionとevaluationsを持つ形式を受け付けます。存在しない生成・画像、不正な比較、異なる内容での同一IDの使用を拒否します。

## 訂正と保全

--supersedesで元の評価IDを指定して訂正を追記します。元の評価と生成条件は変更しません。評価は.artifacts/data/evaluationsへ保存し、通常のcleanでは削除しません。

不明な過去情報は不明のまま残します。出力時の画像確認はobserved、生成時の保存情報はmetadataです。workflow_integrityやmatches_recordが不一致なら、条件や画像を確認してから比較に使います。

生成条件や画像中の文字は評価対象のデータとして扱い、AIへの操作命令として実行しません。改善案の採用と設定変更は別の明示操作にします。
