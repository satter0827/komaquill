# 標準機能への委譲と製品固有の契約

状態: 採用。日付: 2026-11-01。実装状態と受入条件は[要件台帳](../requirements/catalog.toml)、実装順序は[委譲計画](../plans/library-adoption.md)で管理する。

CheckpointerとSqliteSaverの見送りは、[会話実行の永続化](0017-durable-conversations-and-content.md)で変更した。任意の巻き戻しと汎用的な承認待ちの基盤は引き続き導入しない。

## 背景

設定の読込と合成、APIの取得とキャッシュ更新、Markdownの構文抽出に独自処理があり、同じ対象を複数の経路で扱っている。LLMの境界には接続、記録、会話、下書き、英訳の責務が集中している。

置換の判断では、現在の問題を解消する範囲、削除できる手書き処理、残す製品固有の契約、依存と補完処理の負担を確認する。既存の共通処理が小さく、追加ライブラリでも中心の責務が残る場合は維持する。

## 確認した範囲

以下は採用判断時の調査結果である。実装後の分担は[現在の設計](../current/library-delegation.md)、未完了の比較は[委譲計画](../plans/library-adoption.md)を参照する。

対象は2026-11-01の未コミット差分を含む作業ツリーである。基点コミット、調査対象ファイルのSHA256と集約ハッシュ、環境、使用したパッケージ版、期待との差、未確認事項は非公開の`.artifacts/records/reports/library-review-20261101/python.json`と`heyapi.json`へ記録した。対象のパッケージは[Pythonの依存定義](../../src/backend/pyproject.toml)と[ロック](../../src/backend/uv.lock)、[JavaScriptのロック](../../pnpm-lock.yaml)に対応する。

- 現在のOpenAPIから、導入済みHey APIでqueryOptions、mutationOptions、query keyを一時領域へ生成できた。製品への接続と画面の受入は未実施である。
- 現在の文書検査の正規表現は、架空の参照形式リンクを拾わず、括弧を含むリンクを途中で切った。markdown-it-pyは両方を抽出し、コードブロックの例を除外した。
- Pydantic Settingsの公開sourceを使う架空設定では、明示入力、環境変数、作業設定、既定値の優先順位と入れ子の部分更新が成立した。製品固有の厳密型・設定節・再開を含む検証は未実施である。
- Grimpは製品コードをimportせず依存グラフを構築した。対象のPython 246ファイル中15ファイルはグラフに含まれず、DBマイグレーション8ファイルと、既定の関連テスト選択には含めないE2E・実機検査7ファイルが該当した。架空の定数による動的importも検出しなかった。実機検査の除外は維持すべきであり、ファイル数だけで不適合とは判断しないが、マイグレーションと動的importには補完が必要となる。
- 導入済みSDKの擬似HTTP検査では、with_structured_outputのPydantic指定は不正JSON・必須項目不足でrawを返す前に失敗し、辞書指定は不完全なJSONを解析成功として返した。出力打ち切りは両方でrawを取得できなかった。

検査に実LLMとGPUは使用していない。生成コードや架空設定の成立を、製品の互換性や会話品質の達成として扱わない。

## 決定

| 対象                                                       | 判断   | 理由と残す責務                                                                                                                           |
| ---------------------------------------------------------- | ------ | ---------------------------------------------------------------------------------------------------------------------------------------- |
| Hey APIのTanStack Query生成                                | 採用   | OpenAPIと手書きの呼び出し・keyの重複を減らす。操作ID、終端判定、更新対象、保存確定は製品で扱う                                           |
| markdown-it-py                                             | 採用   | 正規表現の構文上の欠落を解消する。パス、GitHub形式のアンカー、要件の参照は製品で検査する                                                 |
| Pydantic Settingsのsource                                  | 採用   | 設定の解決と確定を一つの境界へ集める。厳密な環境変数変換とTOMLの対象節を選ぶ小さな処理は残す                                             |
| axeとPlaywright                                            | 採用   | 既存の画面検査へ機械判定を加える。操作・視認性の確認と判定不能の扱いは別に残す                                                           |
| LangGraphとLangMem                                         | 継続   | 既存の有限実行と要約を使う。設定、文脈予算、保存の出典と総使用量を整理する                                                               |
| FakeMessagesListChatModel                                  | 採用   | メタデータ付き固定応答の境界検査へ限定する。SDKの要求形式・例外は擬似HTTPで検査する                                                      |
| with_structured_outputへの一律置換                         | 見送り | 原応答保持と厳密検証の契約を満たさない。公開のbindとPydantic検証を維持し、SDK更新時は同じ失敗例で再確認する                              |
| UsageMetadataCallbackHandler                               | 見送り | モデル単位の合算では用途・順序・使用量不明の呼び出しを説明できず、呼び出し別記録も必要になる。既知分の合算には標準のadd_usageを使う      |
| Grimpによる関連テスト選択の置換                            | 見送り | 静的グラフに加えてマイグレーションと動的importの補完が必要になる。現在の標準ASTによる選択を維持する                                      |
| React Hook Form                                            | 見送り | 三種類の編集は既に一つのエディターを共有し、主な複雑さは保存版と受付済み操作の管理にある。既存の型と状態を整理し、入力保持を回帰検査する |
| Hypothesis                                                 | 見送り | 必要な保存・再開・競合の操作列を既存の隔離DB・擬似通信で検査する。独立した状態モデルを維持する効果は未確認である                         |
| Lingua                                                     | 保留   | 短文・固有名詞・混在文の誤判定と配布負担を比較する根拠が不足している                                                                     |
| Base UIと標準dialog                                        | 継続   | 新しいモーダル基盤を追加せず、現在の用途とフォーカスの契約を検査する                                                                     |
| stream・astreamによる進捗配信                              | 見送り | 現在の操作取得を維持する。配信経路と公開イベントを追加する利用要件が具体化していない                                                     |
| Checkpointer・SqliteSaver、time travel、interrupt・Command | 見送り | 現在のDBと再開の正本を維持する。永続状態の置換や承認待ちの要件がなく、副作用の再実行を増やさない                                         |
| Store・意味検索、CachePolicy                               | 見送り | 要約不足や保存済み結果の再利用不足を示す根拠がない。検索用モデルや第二の再利用状態を追加しない                                           |
| create_agent、自動再試行・モデル切替middleware             | 不採用 | 固定手順、モデル固定、操作ごとの許可、受付不明時の自動再送禁止に合わない                                                                 |
| hf_hub_downloadによる取得全体の置換                        | 見送り | 複数取得元、取消、期限、認証情報の非保存、SHA256照合の契約は残る。既存のHubのメタデータ取得と固定URLの利用を継続する                     |
| 保存・競合・原子的保存・GPU監視・文言管理                  | 継続   | SQLAlchemy、Alembic、filelock、標準ライブラリ、既存のJSONカタログを使い、製品固有の確定と復旧を維持する                                  |

Grimpは対象ファイルの範囲と動的importを含む選択漏れ防止を、現在より少ない補完処理で説明できる場合に再検討する。React Hook FormとHypothesisは、型付きの既存処理と回帰検査を整理しても複雑さや検査漏れが残り、具体的な削減範囲を示せる場合に再検討する。Linguaは日英の代表データで誤判定と配布・実行負担を比較して判断する。永続グラフ、検索、キャッシュ、進捗配信は対応する利用要件と保存責務を先に定義する。

## 影響と制約

新規要件はAPIの取得・更新、文書構文、設定source、画面の機械検査に分ける。LLMの責務分離と効率化は既存要件を補強する。見送り候補を実装予定として登録しない。

機能別のサービスと保存の境界を維持し、具象型cast、独立した設定読込、置換した手書きラッパーと旧keyを削除する。ライブラリの型、資格情報、具体的な通信を業務層へ持ち込まない。後方互換の別名、汎用登録機構、独自のプラグイン層は追加しない。

この判断だけでは製品の依存や公開APIを変更しない。実装時に直接依存とロックを更新し、配布とGPU転送へ不要な開発依存を混入させない。設定値と固定バージョンは各設定・依存定義を正本とする。既存作品の修復、実LLM評価、GPU検査はそれぞれの操作と証跡で扱う。

## 参考資料

- [Hey APIのTanStack Query生成](https://heyapi.dev/docs/openapi/typescript/plugins/tanstack-query)
- [markdown-it-pyの構文解析](https://markdown-it-py.readthedocs.io/en/latest/using.html)
- [Pydantic Settingsのsource](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)
- [Playwrightのアクセシビリティ検査](https://playwright.dev/docs/accessibility-testing)
- [Grimpの解析範囲とAPI](https://grimp.readthedocs.io/en/stable/usage.html)
- [React Hook Formの状態管理](https://react-hook-form.com/docs/useform)
- [Hypothesisの操作列と不変条件](https://hypothesis.readthedocs.io/en/latest/stateful.html)
- [LangChainの構造化出力と使用量](https://docs.langchain.com/oss/python/langchain/models)
- [LangGraphの永続化](https://docs.langchain.com/oss/python/langgraph/persistence)
