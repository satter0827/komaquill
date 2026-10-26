# 参照画像から編集する

`klein-edit`を有効にしてstartします。App Modeでは参照画像を選び、プロンプトへ変更点を記入して実行します。詳細な条件はNode Graphで調整できます。元画像と同じ構図や対象を維持できるかは生成画像で確認してください。

Python APIでは保存した入力を渡します。

```python
from pathlib import Path
from komaquill.api import Client

client = Client(Path.cwd())
reference = client.inputs.import_file(Path("reference.png"))
run = client.images.generate(
    "Keep the scene and character. Make the umbrella blue.",
    profile="klein-edit",
    inputs={"reference": reference},
    seed=42,
)
print(run.state)
```

CLIではJSON入力を使用します。ファイルの相対参照はJSONファイルのあるフォルダーを基準とします。

```json
{
  "format_version": 2,
  "prompt": "Keep the scene and make the umbrella blue.",
  "inputs": {"reference": "reference.png"},
  "parameters": {"party_strength": 1.0, "fixes_strength": 1.0}
}
```

```powershell
komaquill generate --profile klein-edit --input-file edit.json --seed 42
```

保存した素材、条件、実行グラフは生成記録から参照できます。ストーリーの場面別指定は[Python API](python-api.md)と[契約](../current/story-workflows.md)を参照してください。
