# Stock Report

事件卷宗與候選排序已加入 `event-rank`，詳見 [操作與限制](docs/phase-0/event-ranking.md)。排序依據可用關聯證據，不代表投資方向；目前仍不會自動搜尋全網新聞。`demo` 的事件報告會先經排序再產生 PDF。2330 來源查核包為未啟用參考，使用者已再次選擇維持來源關閉，不需要重複確認。

本機投資研究工具的 **Phase 0 可執行 POC**。依 [產品規格](docs/product-spec-v0.2.md) 與 [驗證計畫](docs/phase-0-feasibility-plan.md) 建立，尚未宣告正式 MVP 或 Phase 0 GO。

已實作 Schema 驗證、Prepare → 提案交換 → Finalize、SQLite 證據庫、Python 確定性計算、機構評等統計、逐公司／逐期限 Policy Gate、兩頁繁體中文 PDF、稽核 JSON／Manifest 與隔離 Replay。合成資料明確標示 `DEMO / SYNTHETIC DATA`，真實資料失敗不會自動退回 Mock。

**所有輸出僅供使用者本人使用，不分享、不散布**（2026-11-15 範圍變更）。PDF 每頁標示「僅供個人使用・不得散布 / Personal use only / Not for distribution」；沒有任何分享功能。

## 快速執行

需要 macOS Apple Container（服務已啟動）或 Docker，以及 Git。所有 Python、字型及 PDF 系統依賴只裝在映像內，不需要在主機安裝第三方函式庫。

```sh
container system start
./scripts/runtime.sh build
./scripts/runtime.sh test -q
./scripts/runtime.sh demo
./scripts/runtime.sh qa
# 選用、需明示的真實來源探查（會發出網路請求）
./scripts/runtime.sh smoke --allow-network
./scripts/runtime.sh test -m network
```

Docker 使用相同 `Dockerfile` 與腳本：

```sh
CONTAINER_ENGINE=docker ./scripts/runtime.sh build
CONTAINER_ENGINE=docker ./scripts/runtime.sh test -q
CONTAINER_ENGINE=docker ./scripts/runtime.sh demo
```

`demo` 實際建立美股、台灣上市、上櫃、事件四份合成報告及一份 Replay，索引在 `reports/demo/index.json`。容器回傳 `/work/...` 對應 repository 下的相同相對路徑。`reports/` 預設不納入 Git。

## 三段研究介面

```sh
./scripts/runtime.sh cli prepare \
  --request examples/stock-nvda.json --provider mock

# 將 RUN_ID 換成 prepare 回傳的 run_id。
./scripts/runtime.sh cli analyze --run reports/RUN_ID
# Codex 讀取研究輸入、analysis-instructions.txt、context.json 及 proposal.schema.json，
# 產生 reports/RUN_ID/ai_proposal.json；不需要額外付費 LLM API。
./scripts/runtime.sh cli finalize \
  --run reports/RUN_ID --proposal reports/RUN_ID/ai_proposal.json

./scripts/runtime.sh cli replay --run reports/RUN_ID
```

僅測試合成管線時可用 `analyze --synthetic` 產生確定性的測試提案；這不代表 Codex／LLM 實測；本次另有 [實際互動十例紀錄](docs/phase-0/codex-interactive-evaluation.md)，明列基線與單批次限制。`finalize` 回傳機器可讀的 `run_id`、`status`、`gate_status`、PDF／JSON 路徑、研究截止時間、覆蓋狀態與警告。失敗回傳非零 exit code 和 `status=failed`。

## 真實資料與可行性

`probe` 是**明確啟用網路**的官方端點探查，記錄擷取時間、回應 Hash、少量目標資料、錯誤與授權未知狀態，不直接產生真實研究：

```sh
./scripts/runtime.sh cli probe --allow-network \
  --user-agent 'StockReportPhase0/0.1' --output reports/phase-0
```

SEC 探查需要自行在 User-Agent 設定符合來源要求的聯絡資訊；沒有聯絡資訊便記錄阻礙，不會取用 Claude／Codex 的登入資料。條款未核對前所有真實探查資料均不得用於個人 PDF、指標或提案。無付費 API、無自動重新登入。

### 官方來源採集（collect → bundle → prepare）

真實證據不需要手寫 JSON。採集先行，研究截止時點**之後**才套用：

```sh
# 1) 採集（以下完整流程需先確認登錄的內部使用權；unknown 只輸出 inspection-record.json）
./scripts/runtime.sh cli collect --source twse-openapi-stock-day-all \
  --ticker 2330.TW --allow-network --output reports/collections
# 台灣損益表摘要（一般業，最新一期年初至今累計）：twse-openapi-income-general／tpex-openapi-income-general
./scripts/runtime.sh cli collect --source twse-openapi-income-general \
  --ticker 2330.TW --allow-network --output reports/collections
# SEC 需要操作人自行設定聯絡資訊，絕不取自登入資料，也不寫入任何輸出檔
STOCK_REPORT_SEC_CONTACT='Your Name you@example.com' \
  ./scripts/runtime.sh cli collect --source sec-xbrl-companyconcept \
  --ticker NVDA --allow-network --output reports/collections

# 2) 產生研究截止時點不早於採集時間的 Request
./scripts/runtime.sh cli request-template --ticker NVDA --exchange NASDAQ \
  --collection reports/collections/COLLECTION_ID --output reports/collections/request.json

# 3) 套用時點與授權，輸出證據包；不合格只輸出 bundle-report.json 與 blockers
./scripts/runtime.sh cli bundle --request reports/collections/request.json \
  --collection reports/collections/COLLECTION_ID --output reports/collections/bundle.json

# 4) 進入既有管線（不繞過 Gate）
./scripts/runtime.sh cli prepare --request reports/collections/request.json \
  --provider import --evidence-bundle reports/collections/bundle.json
```

授權狀態來自版本化登錄檔 `config/source-licenses.json`：`unknown`／`restricted` 不可用於個人報告也不可本機留存，
`confirmed` 必須附人工逐字條款引用與具名 attestation。**目前六個來源都是 `unknown`**，
因此 collect 目前只保存檢視用中繼資料，不能把該檔傳入 request-template／bundle 當成研究輸入。
內部權限確認後需重新採集，才會產生 collection.json；核心證據不足時可輸出不足摘要。公開時間依來源分為
來源宣告日期與「擷取當下可得性上界」；後者只適用最新時點研究，既有歷史時點不會被改寫。
欄位契約與限制見 [來源可行性](docs/phase-0/provider-feasibility.md)。

已人工查證身份、發布時間與權限的標準化真實證據也可直接經 `prepare --provider import --evidence-bundle FILE --request REQUEST` 載入。Bundle 使用 `ResearchInput` Schema，必須 `dataset_kind=real`；不得含 Synthetic 證據，且須確認內部保存權限。這是受信任的人工整理介面，**不是自動證實來源真實性或授權的機制**。`schema --output reports/schemas` 可輸出契約。

### 個人 PDF 與本機原始稽核資料的權限

個人使用**不等於**已取得權限，各權限分開判斷：

| 權限 | 作用 |
| --- | --- |
| `internal_use_allowed`（需 `confirmed`） | 來源數值可寫入本機研究目錄，並用於個人 PDF、指標與提案 |
| `store_response_hash`／`store_raw_response` | 本機原始稽核留存（回應雜湊／原始回應）；不因 PDF 可用而自動允許 |
| `pdf_share_allowed`／`shareable_in_pdf` | 舊版再散布欄位，僅為相容保留；不是報告條件，系統不會自行設為 true |

內部使用已確認但再散布欄位為 false 的證據可進入個人 PDF；個人報告不需要再散布簽認。`sharing_profile` 只接受 `private`。PDF 與本機 JSON／SQLite 稽核檔都不得對外提供。

操作細節、已實作範圍與限制見 [實作說明](docs/phase-0/implementation.md)、[來源可行性](docs/phase-0/provider-feasibility.md) 及 [Phase 0 Review](docs/phase-0/phase-0-review.md)。
