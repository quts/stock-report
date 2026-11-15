# 個股研究 V2：最新財報、媒體來源與四頁格式

> 依據：使用者於 2026-11-15 明示的四項產品變更（DELL 研究）。本文件是這四項變更的操作契約；
> 與 [產品規格](../product-spec-v0.2.md)、[Phase 0 計畫](../phase-0-feasibility-plan.md) 衝突時以本文件為準。
> 仍為 Phase 0，尚未通過正式 MVP GO。

## 1. 四項變更

| # | 變更 | 實作 |
| --- | --- | --- |
| 1 | PDF 改為 1–4 頁 | `Request.max_pdf_pages`：整數 1–4，預設 4。設 `2` 仍走舊的兩頁版型（相容）。 |
| 2 | 最新一期財報為完成研究的必要條件 | `LatestFinancialReview`／`FilingDiscovery`、`financial_review_state()`、季別感知的 `statement_metrics` |
| 3 | 主流媒體經查核後可作為 secondary 來源 | `reviewed_collect.py`：`sec-submissions-history`、`reviewed_html`（發行人新聞稿）、`reviewed_rss`（媒體 RSS） |
| 4 | 易讀的四頁研究格式 | `templates/research-four.html`、`Proposal.company_research`、`validate_company_research()` |

## 2. 最新財報（必要）

真實資料、`mode=stock` 且 `max_pdf_pages >= 4` 時，`finalize` 必須通過下列檢查，否則**不產出報告**：

1. `latest_financials.discovery` 來自登錄檔已開放的 `sec-submissions-history`：CIK 與 `config/sec-entities.json` 一致，
   `as_of` 等於研究截止時點，`discovered_at >= as_of`，`published_at <= as_of`（不 lookahead），
   `document_url` 位於該 CIK／accession 的 EDGAR Archives 路徑下。
2. 選取規則是截止時點前**最新的申報期別**。較晚申報的舊期別修正版（如 10-K/A）不會取代較新的 10-Q；
   同一期別優先採原始申報。
3. 財務證據必須來自該申報文件、`period_end` 與申報期別相同，且是 `statement_kind=fact`。
   必要項目為 `revenue`、`net_income`、`operating_cash_flow`。若是 10-Q，另需單季（`single_quarter`）的 `revenue`。
   年報數字或年初至今累計數字，不能冒充最新季度。
4. `earnings_preview` 必須存在且 `status != not_run`（`completed` 或 `unavailable` 皆可，`unavailable` 要寫 limitations）。

阻礙代碼：`LATEST_FINANCIAL_REPORT_MISSING`、`LATEST_FINANCIAL_REVIEW_INVALID`、
`LATEST_FINANCIAL_DISCOVERY_UNVERIFIED`、`LATEST_FINANCIAL_STATEMENTS_INCOMPLETE`、`LATEST_QUARTER_MISSING`、
`EARNINGS_PREVIEW_SEARCH_MISSING`。這些代碼寫在 research.json 的 `completion_blockers` 與 decision.json 的 findings
（方向會降為 insufficient），不會出現在 PDF 正文。

### 2.1 台股（TWSE／TPEx）

台股沒有和 SEC 10-Q／10-K 相同的申報清單，因此最新財報的時效改依**法定公告期限**判斷（證券交易法第36條：
第一至三季於季終了後四十五日內、年度財報於會計年度終了後三個月內公告申報；
https://law.moj.gov.tw/LawClass/LawSingle.aspx?pcode=G0400001&flno=36）。

- `tw_required_period_end(cutoff)`：以台北日期計算，期限當日完全結束後，該期才算「應已公告」。
  只支援 12 月結束的會計年度（依 `config/tw-instruments.json`），其他會計年度判定為 `LATEST_FINANCIAL_REVIEW_INVALID`。
- 截止前可用的官方 `fact` 財務證據（`financial_metric` 為 `revenue`、`net_income`），最新期別不得早於應公告期別，
  否則為 `LATEST_FINANCIAL_REPORT_STALE`；最新期別還必須同時有營收與淨利。年初至今累計或單季皆可。
- 來源：TWSE 綜合損益表 OpenAPI（2026-11-15 AI 查核開放，registry 0.6.2；金額級距未宣告，只作期間覆蓋與同紀錄比率），
  以及發行公司向 SEC 申報的 6-K／20-F（`config/sec-entities.json` 將 2330.TW 對應到台積電 CIK 0001046179；
  6-K 有明示單位、單季數字與現金流量）。
- 台股官方開放資料集沒有現金流量表；沒有可用的現金流量證據時，報告的資料範圍會寫明。

## 3. 前瞻陳述的分類

`Evidence.statement_kind`：`fact`、`reported_claim`（媒體報導的說法）、`management_guidance`、`analyst_estimate`、
`consensus_estimate`、`scenario`。

- guidance／estimate 必須有 `target_period`；`analyst_estimate`／`consensus_estimate` 必須有具名的 `institution`。
- `EarningsPreviewReview.expectation_evidence_ids` 只能引用 guidance 或具名 estimate；所有引用都必須是同一標的、
  截止時點前已公開的資料。`next_earnings_at` 必須晚於截止時點。
- 查不到可靠的市場共識時，就只寫公司指引與應觀察項目，不能捏造預測。`scenario` 是自行推演，PDF 不會把它列為外部預期。

## 4. 主流媒體與發行人新聞稿

- 媒體一律標為 `source_class=secondary`，RSS 項目標為 `statement_kind=reported_claim`，並保留 `article_url`；
  只保存標題與摘要，不保存全文、原始回應或雜湊。
- 收錄規則：來源必須在登錄檔中為 `source_reviewed` 且 `internal_use_allowed`，**否則在發出任何網路請求前就拒絕**。
  publisher 身份與 channel title 要一致；不收錄截止前尚未發布的項目；拒絕連到他站的連結以及 DTD／ENTITY。
- 發行人新聞稿（`reviewed_html`）：只能使用登錄的端點，頁面必須含有發行人身份字串，每段逐字摘錄都要出現在即時頁面中；
  發布日期依審閱者核對的日期建模。
- 來源探查（`news-discover`）和事實採集是分開的：探查結果只是線索，不能直接引用。

## 5. 四頁格式

`finalize` 選用 `research-four.html` 的條件：個股研究、`max_pdf_pages >= 4`，且提案含有 `company_research`。

| 頁 | 內容 |
| --- | --- |
| 1 | 公司如何賺錢、研究重點、最新季度部門表現 |
| 2 | 最新一期財報：單季實績與去年同季、資產負債與資金占用、基本面分析 |
| 3 | 下一次財報日期、公司指引／具名外部預期（分欄標示）、近期消息與影響路徑、所有已查核的重大事件 |
| 4 | 3-6m／6-12m 分析與追蹤條件、驗證清單、單一的資料範圍說明、編號來源索引 |

- 正文用 `[n]` 註腳，不放內部 Evidence ID；Gate 代碼和阻礙代碼只留在 JSON。
- 估值資料不足時，公司方向仍會降為 insufficient，但已引用證據的業務分析照常呈現。
- 真實四頁報告必須填滿 `company_research` 的六個段落：overview、key_takeaways、fundamentals、news、earnings_preview、monitoring。
  每段的 evidence_ids 都必須是該公司、截止前、已開放內部使用的證據，否則 finalize 失敗。

## 6. CLI 操作

```sh
# 1) 最新申報發現（需 SEC 聯絡資訊由操作人提供）
echo '{"as_of": "2026-11-15T08:53:54Z"}' > reports/RUN/discovery-plan.json
scripts/runtime.sh cli collect --source sec-submissions-history --ticker DELL --allow-network \
  --source-profile reports/RUN/discovery-plan.json --output reports/RUN/collections

# 2) 最新申報的財務事實（FilingPlan，facts[].kind=financial，含 financial_metric、numeric_text、
#    period_start/period_end/period_basis；numeric_text 必須在逐字摘錄內）
scripts/runtime.sh cli collect --source sec-edgar-archives-filing --ticker DELL --allow-network \
  --source-profile reports/RUN/financial-filing-plan.json --output reports/RUN/collections

# 3) 發行人新聞稿（下一次財報日期、公司指引）與媒體 RSS
scripts/runtime.sh cli collect --source dell-ir-next-earnings-1661 --ticker DELL --allow-network \
  --source-profile reports/RUN/earnings-schedule-plan.json --output reports/RUN/collections
echo '{"keywords": ["Dell"]}' > reports/RUN/rss-plan.json
scripts/runtime.sh cli collect --source lemonde-economy-rss --ticker DELL --allow-network \
  --source-profile reports/RUN/rss-plan.json --output reports/RUN/collections

# 4) 套用截止時點並附上下一次財報預期的搜尋紀錄（EarningsPreviewReview JSON）
scripts/runtime.sh cli bundle --request reports/RUN/request.json \
  --collection reports/RUN/collections/ID1/collection.json --collection ... \
  --earnings-preview reports/RUN/earnings-preview.json --output reports/RUN/bundle.json

# 5) 事件查核、prepare、Codex 產生含 company_research 的 ai_proposal.json、finalize
scripts/runtime.sh cli stock-events --bundle reports/RUN/bundle.json --dossier reports/RUN/dossier.json --output reports/RUN/reviewed-bundle.json
scripts/runtime.sh cli prepare --request reports/RUN/request.json --provider import --evidence-bundle reports/RUN/reviewed-bundle.json --output reports/RUN/runs
scripts/runtime.sh cli finalize --run reports/RUN/runs/RUN_ID --proposal reports/RUN/runs/RUN_ID/ai_proposal.json
```

`earnings-preview.json` 範例：

```json
{"status": "completed", "as_of": "2026-11-15T08:53:54Z", "searched_at": "2026-11-15T09:10:00Z",
 "search_queries": ["Dell third quarter fiscal 2027 conference call"],
 "sources_checked": ["https://investors.delltechnologies.com/news-events/press-releases"],
 "next_earnings_at": "2026-11-24T21:30:00Z", "schedule_evidence_ids": ["dell-ir-call"],
 "expectation_evidence_ids": [], "limitations": ["未找到可驗證的具名市場共識。"]}
```

bundle-report.json 新增 `latest_financial_report_status` 與 `earnings_preview_status` 兩個欄位，可在 prepare 前檢查。

## 7. 未解決事項

- 台股時效檢查只支援曆年制；其他上市櫃公司若無 SEC 申報，現金流量與單季數字需另找已查核來源。
- `reviewed_html` 只登錄了 DELL IR 單一頁面；其他發行人需要另外查核並登錄。
- 媒體來源目前只開放 Le Monde Economy RSS。其他主流媒體的條款查核結果尚未確認，維持封鎖。
