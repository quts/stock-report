# Phase 0 Review

## 決策

**尚未達到正式 MVP GO；保留可執行 POC 繼續驗證。** 下列是能力別狀態，不宣稱任何預測表現。

| 能力 | 決策 | 已有證據／阻礙 |
| --- | --- | --- |
| 合成 US / TWSE / TPEx 股票管線 | POC 可用 | 固定測資驗證 Schema、身份、計算、雙期限、缺評等 PDF |
| 合成跨市場事件管線 | POC 可用 | 最多五家限制、直接／間接／假設、逐公司 Gate 與兩頁摘要 |
| Gate / Replay | 固定資料驗證 | 故障注入、逐期限降級、輸入及成品 Hash、隔離重播；門檻尚未校準 |
| 真實 TWSE / TPEx | 正式發布 NO-GO | 官方端點取得目標資料，但分享授權與完整核心研究資料尚未驗證 |
| 真實 US | 正式發布 NO-GO | SEC 未配置操作人聯絡 User-Agent；行情／授權與真實 E2E 未完成 |
| 逐機構評等 | 尚無真實覆蓋 | 可測試映射與去重，不捏造免費資料來源 |
| Codex 互動提案十例 | NOT_RUN | 已實作交換契約，但 deterministic 合成提案不等於 LLM 實測 |
| Docker 引擎 | 相容設計，未實測 | 同一 Dockerfile 與腳本；實際驗證引擎為 Apple Container |

## 測試證據與範例

執行 `scripts/runtime.sh test -q --junitxml=reports/phase-0/junit.xml` 保存確切通過／失敗數。執行 `demo`、`qa` 與 `bundle` 保存四份合成研究及 Replay、版型 PNG、Golden 故障預期／實際、覆蓋、Manifest、十個未執行 LLM 情境及實際來源探查。測試成功數以當次 JUnit 為準，不硬編碼為永遠通過。

Review Bundle 路徑為 `reports/phase-0/`；`sample-stock.pdf`、`sample-event.pdf` 均為明顯標記的 DEMO，不得作為真實投資研究。PDF 檔案需經轉圖檢查字型、頁數與截斷；自動測試另檢查 A4、中文字、資料不足揭露、兩期限及權限內容排除。

本機 Claude Code 已嘗試在沙箱外執行，但受到帳號五小時用量上限而停止，核心程式由 Codex 接手完成；這不是研究 LLM 的十例驗證。未採購 API、未在主機安裝依賴、未變更登入設定。

## 完成 Phase 0 的具體條件

1. 操作人配置 SEC 所需聯絡資訊；完成台美核心資料、發布時間與逐來源內部／分享授權紀錄。
2. 增補正式正規化 Provider，完成 NVDA 與 2330 真實研究 E2E；AMD／6488 依覆蓋揭露限制。
3. 使用 Codex 互動模式逐一跑十個固定情境，保存實際提案、失敗、模型／Prompt、耗時、用量及人工介入紀錄。
4. 校準並凍結核心證據門檻，人工核對自由文字推論與原始主張的語意。
5. 實測一個真實跨市場事件及其候選排序，不將相關公司直接稱為受益公司。
6. 完成真實股／事件 PDF 權限及版面驗收，再依市場分別決定 GO / CONDITIONAL GO / NO-GO。

未達到上述條件前，不擴展為無人值守或可分享的正式研究服務，不以合成範例補真實缺值。
