# Phase 0 實作與容器操作

## 環境與版本控制

實作分支 `codex/phase-0`。使用 Python 3.11、Pydantic v2、SQLite、Jinja2、WeasyPrint、pytest；Python 與 CJK 字型均安裝在容器內。`Dockerfile` 同時適用 Apple Container、Docker；`scripts/runtime.sh` 預設使用 Apple Container，`CONTAINER_ENGINE=docker` 切換 Docker。Docker 尚未在此環境實測，不宣稱雙引擎驗證均已通過。

```sh
./scripts/runtime.sh build
./scripts/runtime.sh test -q --junitxml=reports/phase-0/junit.xml
./scripts/runtime.sh demo
./scripts/runtime.sh qa
./scripts/runtime.sh bundle
# 明示的真實來源探查（選用，會發出網路請求）
STOCK_REPORT_SEC_CONTACT='Your Name you@example.com' ./scripts/runtime.sh smoke --allow-network
./scripts/runtime.sh test -m network
```

建置需要網路取得基礎映像及依賴；一般測試與 Mock 管線不呼叫市場資料或模型 API。依賴直接版本固定在 requirements.txt，間接依賴尚未完整鎖定；Manifest 記錄程式與模板雜湊，但不主張跨任意映像可逐位元重製。

## 固定執行契約

`news-discover --request --output` 支援固定新聞索引端點的有界查詢及未核實公司提及；真實模式必須明示 `--allow-network`，來源未核對時僅保存狀態與數量。離線完整線索須明示合成 Fixture。詳細限制與實際 HTTP 429 紀錄見 [新聞探索](news-discovery.md)。搜尋輸出不能直接當成事件卷宗或研究證據匯入。

事件增量支援 `event-rank --dossier` 與唯讀 `activation-check --package`。前者只排序卷宗中有證據的有界候選集合，細節見 [事件卷宗與排序](event-ranking.md)，不聲稱自動搜尋新聞或全市場探索。`prepare --provider synthetic-event` 只接受明示的合成事件證據包；真實資料仍只走 import。事件選取說明更新後 Template 為 4；Prompt 為 2。

`prepare --request JSON --provider mock|import [--evidence-bundle JSON] --output reports` 驗證 Request，產生 UUID 研究目錄、Evidence SQLite、JSON、提案 Schema、Prompt、輸入雜湊。匯入數值會重新計算，不能信任外部提供的 Quant 結果；不得把合成資料標為真實資料。

`collect --source SOURCE_ID --ticker TICKER [--allow-network | --fixture JSON] [--license-registry JSON]
[--source-profile JSON] [--sec-contact TEXT] --output reports/collections` 在任何研究截止時點之前採集官方來源紀錄。
真實採集必須明示 `--allow-network`，失敗時直接報錯，沒有 Mock 或合成替代。`--fixture` 只用於離線契約測試，
會標記 `capture_mode=fixture`，預設不被 `bundle` 接受；即使使用測試旗標，輸出仍為 mock／synthetic，不能當成 real 匯入。SEC 聯絡資訊僅能由 `--sec-contact` 或
`STOCK_REPORT_SEC_CONTACT` 提供，不取自登入資料，也不寫入採集檔或錯誤訊息。

`bundle --request JSON --collection DIR [--collection DIR ...] --output bundle.json` 才套用研究截止時點，
產生可供 `prepare --provider import` 使用的真實證據包。授權未確認、公開時間不支援該時點、未通過核實或
身份不符的證據會被逐筆排除並記錄原因。身份缺失等不可還原問題只輸出
`bundle-report.json` 與 blockers（結束碼 1）；核心來源紀錄少於兩筆則保留 coverage warning，
允許不足摘要，由 Gate 阻擋方向性結論。供應的 `research_as_of` 永遠不會被改寫。

`request-template --ticker TICKER [--exchange X] --collection DIR --output request.json` 以採集時間作為預設
研究截止時點；對 `latest_only` 來源拒絕早於採集時間的時點，且拒絕覆寫既有 Request 檔。

`smoke`（`./scripts/runtime.sh smoke --allow-network`）是明示的真實來源探查，輸出
`reports/phase-0/source-smoke.json`；`./scripts/runtime.sh test -m network` 另有同樣需明示的契約測試
（預設以 `-m 'not network'` 排除）。`./scripts/runtime.sh contract --allow-network --endpoint KEY [--code 2330]` 只在終端輸出白名單端點的欄位名稱、列數與字元類別形狀，不寫檔；`--cumulative-check twse|tpex` 只輸出損益表營收與月營收累計數的比值。未知內部保存權限時，兩者只保存欄位名稱、狀態與數量等檢視中繼資料，不保存來源數值，也不建立授權結論。

`analyze --run DIRECTORY` 只輸出受控交換檔；Codex 在互動環境依證據產生提案。此命令不使用付費 API、不假裝已呼叫模型。`--synthetic` 僅允許 Mock，產生清楚標示為合成的測試提案。此版本沒有任意自然語言 shell 執行功能。

`finalize --run DIRECTORY --proposal JSON [--rating-map config/ratings.json]` 檢查輸入 Hash、提案 Schema、每家公司每期限唯一提案、Policy、內部使用權限過濾及 PDF 頁數。輸出 PDF 前先寫暫存檔；失敗不宣稱完成。已完成的研究目錄不可直接覆寫。

`replay --run DIRECTORY --output reports` 驗證來源檔案 Hash 與程式／模板／Policy／映射版本，在新 UUID 目錄重播，要求決策結果雜湊一致。此版本不支援從較舊程式自動重建舊環境；版本不符便拒絕。Fresh Run 必須重新 prepare。

容器 `/work` 綁定主機 repository；例如 `/work/reports/abc/report.pdf` 的主機路徑為 `<repository>/reports/abc/report.pdf`。

## 研究模型與計算

身份包括 ticker、exchange、currency、share_class；時間使用時區感知 datetime。區分事件、公開、擷取、研究截止及授權查證時間。每筆 Evidence 保留來源 URL、來源 ID、發布者、原始主張、期別、原始評等及權限。

公開時間另以 `publication_basis` 明示依據：`source_declared_timestamp`、`source_declared_date`
（只宣告日期，`published_at` 取該日時區日終作為上界）、`observed_availability`
（來源未宣告公開時間，`published_at` 僅為擷取當下可得性上界，另存 `availability_observed_at`）、
`synthetic`。`source_published_raw`、`source_period_raw`、`source_unit_raw` 保留來源原文，
與正規化後的 `period`／`unit`／`metric_key` 分開。擷取時間不會被當成公開時間使用，
`observed_availability` 證據不得用於早於觀測時間的歷史時點研究。

普通股身份另依 `config/tw-instruments.json` 明示的 POC 標的設定，不以代號形狀推定。

來源授權由版本化登錄檔 `config/source-licenses.json` 決定：`unknown`／`restricted` 不可用於個人報告也不可本機留存，
`confirmed` 需要人工逐字條款引用與具名 attestation 才能通過驗證。Evidence 另存
`license_source_id` 與 `license_registry_version` 以供追溯。

`metric_key` 為正規化的計算角色，與來源本身的 `source_record_id` 分開。現階段支援相鄰 FY 營收／EPS 成長、同期期別淨利率、營業現金流減正規化資本支出的自由現金流、正 TTM EPS 本益比。不同幣別／單位、不可比期別、非正分母或缺值不計算；缺值不是零。TTM 成長、P/S、EV/EBITDA、負債指標、調整後歷史價格及同業比較未實作，不以替代數字冒充。

評等映射經版本管理，保留原詞彙與機構、分析師、訊息數的不同分母。同機構取截止日前最新有效紀錄；有效期限未知不當成仍有效，未定義詞彙列 Unclassified。樣本不足時仍輸出 PDF，顯示「投顧共識資料不足」。

## Gate 與個人報告邊界

2026-11-15 起（Policy `phase0-2`、模板 `2`）所有輸出僅供使用者本人使用，不分享。Gate 與 Renderer 只檢查已確認的內部使用權限（`license_status=confirmed` 且 `internal_use_allowed`）；`shareable_in_pdf`／`pdf_share_allowed` 為相容用舊欄位，不參與任何判斷，Mock 與測試登錄檔一律為 false。`prepare` 另拒絕無內部使用權限或授權狀態非 confirmed 的匯入證據。Request 的 `sharing_profile` 只接受 `private`。

預設零次自動修正：不循環呼叫模型；相關期限違規直接降級 insufficient 並保留規則。一次修正是規格初始建議而非必須開啟的能力。`REVISE` 保留在狀態 Schema，目前沒有自動修正流程。

Hard Gate 包括未知 Evidence ID、錯誤標的、截止日後資料、未核實／矛盾主張、內部使用權限未確認（unknown／restricted）、Quant 重算不符、核心證據不足、未解決分歧及無法核實的事件關聯。錯誤主要限於公司／期限；身份或 PDF 核心流程不成立才不產報告。所有期限方向被拒仍可生成清楚標記的資料不足摘要。

Phase 0 核心門檻為兩個不同來源紀錄及反轉條件，**尚未校準**。相同發布者另附單來源警告，不稱為獨立交叉驗證。成長趨勢與 AI 方向相反須具體解釋，否則降級；不直接平均為 Neutral。Hypothetical 關聯不發布方向性結論。

Gate 能驗證結構、時點、數值、引用與輸入權限，不能證明自由文字推論與來源語意一致，也不能證明預測正確。真實輸入中的 verified／confirmed 標記須由人工來源查核支持；全自動真實研究尚未驗收。

PDF 只接收 Gate 結果和已確認內部使用證據的投影，每頁正文頂端標示「僅供個人使用・不得散布 · Personal use only / Not for distribution」，首頁另有不得轉寄的說明，模板自動跳脫 HTML，禁止 Renderer 讀取本機／遠端 URL 資源。超過兩頁直接失敗，不縮成極小字體。頁尾採簡潔頁碼；使用限制與風險揭露放在正文，避免頁首頁尾渲染漏印必要揭露。長敘述用明示摘要，完整本機 JSON 保留。報告為 A4，股／事件兩版，分列 3–6、6–12 個月。目前 Quant 為 2、Prompt 為 2、Template 為 4，Context 的版本取自同一執行版本紀錄。

## 儲存、授權與安全

`reports/<run_id>/` 包含 report.pdf、research.json、evidence.json、evidence.sqlite、decision.json、ai_proposal.json、rating_mapping.json、research_input.json、manifest.json。Manifest 記錄 `distribution=personal_use_only`、`personal_report_files=[report.pdf]`，舊鍵 `shareable_files` 固定為空。PDF 與原始／稽核 JSON、SQLite 都是個人本機資料，均不得對外提供。個人 PDF 的權限（`internal_use_allowed`）與本機原始稽核留存（`store_response_hash`／`store_raw_response`）分開判斷：前者不代表可保留回應雜湊或原始回應。預設不保存網頁全文或第三方研究報告。

HTTPClient 提供有限 Retry、速率限制與可選 TTL Cache；原始回應 Cache 預設停用，僅在來源內部保存授權確認後由 Adapter 顯式啟用。探查只保存目標欄位及回應 Hash，不保存全文。CLI 不執行來源文字、不組 shell、不讀取登入憑證。User-Agent 聯絡資訊由操作人自行配置，不能從登入資料取得。

## 尚未驗收的必要項目

真實台美財報／行情正規化與內部使用條款、逐機構評等來源、跨市場真實事件探索排序、Codex 十例互動提案驗證、真實股／事件兩頁報告、校準 Gate 門檻。這些狀態必須留在 Review，不能用 Mock 測試通過取代。

真實採集管線（`collect`／`bundle`／`request-template`、授權登錄檔、公開時間模型）已實作並附離線契約測試，
但**尚未以實際網路回應驗證**，且**本次新增的測試尚未在容器內執行**：本工作階段未取得執行容器、
Python 或網路工具的授權。欄位契約、來源條款與測試結果都必須在下一次實際執行後才可視為成立。

## 本次互動驗證紀錄

Review Bundle 載入並驗證 `reports/codex-evaluation/llm-evaluation.json` 的歷史十例成品雜湊，保留基線 commit 與單批次限制；不冒充對目前程式版本重新呼叫模型。只有版本化摘要時會標示原始成品不在本機，無紀錄時才用 NOT_RUN。完整說明見 [Codex 互動驗證](codex-interactive-evaluation.md)。

台股來源的 Date 僅作原始交易期別 `source_period_raw`；未宣告的公開日期 `source_published_raw` 保持 null，不把交易日放進公開日欄位。使用者本次選擇維持來源 unknown，未啟用來源數值保存或使用。
