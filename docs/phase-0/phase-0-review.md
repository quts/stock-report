# Phase 0 Review

## 目前決策

2026-11-15 追加人工核對前查證：已完成 2330／6488 官方指定財報 CSV 與 JSON 的六個目標欄位比對；已整理公司財報單位／年度依據及 Yahoo／SEC 的官方文件與存取限制。全部結果、可核對範圍與不建議啟用項目見 [前置查證結果](pre-human-review.md)。未知欄位不要求人工猜測，來源權限不變；不再次要求啟用。

**Phase 0 程式與離線驗收已完成；正式真實研究維持 NO-GO，尚未達正式 MVP GO。** 使用者已明示保持來源關閉，真實研究不啟用，也不再列為要求使用者反覆確認的待辦。離線軟體驗收、實際端點契約與正式真實研究分開記錄，不能互相替代。完整驗收可用 `scripts/runtime.sh verify` 一次執行，狀態見 `reports/phase-0/acceptance.json`。

**2026-11-15 範圍變更**：使用者明示所有輸出僅供本人使用、永不分享。再散布權不再是報告條件；內部使用已確認（即使再散布欄位為 false）的證據可進入個人 PDF、指標與提案。登錄檔權限未變更，六個來源仍為 unknown 並維持封鎖。

| 能力 | 狀態 | 實際證據與限制 |
| --- | --- | --- |
| US／TWSE／TPEx 合成股與事件管線 | 已驗證 | 雙期限、缺資料降級、兩頁 PDF、JSON、Gate 與 Replay |
| 官方來源採集與正規化 | 已實作並測試 | collect → request-template → bundle → prepare；版本化授權、來源範圍綁定、日期與身份契約 |
| 個人使用報告（不分享） | 已實作並以合成資料與虛構登錄檔驗證 | PDF 每頁標示僅供個人使用・不得散布；Gate 只看已確認的內部使用權限；真實來源仍 unknown，尚無真實個人報告 |
| 匯入事件卷宗與有界候選排序 | 已實作並以合成卷宗驗證 | 依直接／間接／假設與獨立來源數排序，明列支援範圍、排除理由、上限與新聞窗口；尚非自動新聞搜尋或全市場探索。見 [操作與限制](event-ranking.md) |
| 自動新聞索引與公司提及線索 | 已實作並以離線契約驗證；網路 NO-GO | 固定 GDELT DOC 2.0 查詢與有界別名匹配；2026-11-15 真實網路測試 HTTP 429 失敗。搜尋只產生未核實線索，不會轉為 Evidence。來源關閉時只保存契約／數量／狀態。見 [操作與限制](news-discovery.md) |
| NVIDIA 官方新聞稿 RSS | 實際網路契約通過；來源權限未啟用 | 固定官方 Feed，明示 --source 選取，沒有隱藏 fallback；日期、XML、筆數、去重及不留存已驗證，不把官方 RSS 當成內容已核實或取得使用權。見 [操作與驗收](operations.md) |
| 機構評等統計 | Quant 2 已實作並測試 | 正規化去重、機構映射、同時點衝突未分類、分析師／機構／消息分開計數；無真實評等 Provider，不保證覆蓋率 |
| TWSE 2330／TPEx 6488 | 網路契約已驗證；真實報告仍 NO-GO | 實際端點回應可解析，兩者目前日期欄位為民國格式；權限 unknown，只保存檢視用中繼資料 |
| 台灣財報（綜合損益表，一般業）2330／6488 | 離線與網路契約已驗證；真實報告仍 NO-GO | 僅最新一期年初至今累計的營收、歸屬母公司淨利、基本 EPS；保留年度／季別原值、推得的期間起訖與 `cumulative_ytd` 基礎。來源未宣告金額級距、累計基礎與申報時間，故只可得同期淨利率；年增率、PE、自由現金流明示省略。權限 unknown，只保存檢視用中繼資料。見 [財報來源查核紀錄](taiwan-financial-source-notes.md) |
| SEC NVDA／AMD | 離線契約已驗證；網路未驗證 | CIK、名稱、年度申報與原始單位已測試；操作人尚未提供 SEC 聯絡資訊，網路測試明確跳過 |
| 來源授權 | 已有助理查核證據，尚無人工簽認 | 官方資料集 11549／11371 的授權標示已核對；CSV／JSON 目標欄位已實測一致；人工查證仍須完成，未自行解鎖權限 |
| Codex 互動十例 | 舊版與目前版本分開驗證 | 本次讀取現行交換輸入後撰寫二十個期限提案，抓到並修正 Prompt 2 Context／Prompt 1 Schema 不一致；新版十例 Schema／Gate／PDF 符合預期，非十次獨立模型呼叫。舊紀錄保留，詳見 codex-interactive-v2.json |
| 投顧評等、全市場真實事件探索 | 尚缺已驗證 Provider | 有界新聞搜尋不代表完整探索或來源驗收；不以三大法人或合成資料替代，不承諾全市場覆蓋 |
| 美股次級行情 | 日線契約與兩項網路測試通過 | 來源維持 unknown；調整口徑未知，不計算真實跨期漲跌幅，並非完整歷史資料驗收 |
| 台灣資產負債表、現金流量表、歷史期別、非一般業格式 | 未實作 | 台灣財報目前只有上列損益表最新一期；沒有前期數與現金流，無法支援成長率或自由現金流 |
| Docker 引擎 | 相容設計，未實測 | 同一 Dockerfile／腳本；實際建置與執行引擎為 Apple Container |

## 可驗收的行為

- 內部使用已確認、再散布欄位為 false 的證據可用於個人 PDF；unknown、restricted 或無內部使用權限的證據不進 PDF、指標或結論，`prepare` 拒絕匯入。
- 觀測可得性時間在 HTTP 回應完成後記錄，不能冒充來源精確公開時間；歷史截止時點不會被改寫。
- 真實來源沒有內部保存權限時，拒絕落盤來源數值，只允許 `inspection-record.json` 的欄位名稱、狀態、數量與限制。
- 端點設定及載入後的來源 URL 都受授權範圍限制；變更資料集不能繼承舊權限。
- Fixture 即使明示允許參與契約驗證，仍標為 mock／synthetic，不會產生 Real Data 研究包。
- 身份及權限已確認、但只有單筆收盤價時，可保留不足摘要；兩筆來源紀錄是「方向性結論」門檻，不是產生任何報告的門檻。
- 財務數字帶期間起訖與基礎；同期比率要求起訖相同，年增率要求兩期為相鄰的完整會計年度。期別標籤或結束年度相同不構成可比，累計或年度 EPS 不年化、不當作 TTM。
- 台灣財報的會計年度結束月份與財報格式來自明示標的設定；缺設定即拒絕，不預設曆年制。
- 台股股類由明示的 POC 標的設定取得，不以四位數代號推定普通股。擴充標的仍需身份／股類證據。

## 實際測試與交付紀錄

- `reports/phase-0/provider-increment-junit.xml`：最新完整離線回歸為 433 passed、10 deselected，包含 Prompt 版本交換的回歸測試。`scripts/runtime.sh verify` 全套完成：測試、四份合成報告及 Replay、PDF 預覽與 Review Bundle。新版實際互動批次另驗十例，不併作獨立模型呼叫數。`reports/phase-0/acceptance.json` 記錄 offline_acceptance_passed、來源全關閉及正式 MVP GO=false。
- `reports/phase-0/rss-network-junit.xml`：官方 NVIDIA 新聞稿 RSS 的真實網路契約 1 passed。另以受控 NVDA Request 實際執行 CLI，20 項 Feed 中有 18 項符合窗口的候選統計；只保存數量與狀態，沒有保存標題、文章連結、原始回應或雜湊，紀錄為 `reports/news/nvda-feed-inspection.json`。這不是已啟用的研究證據。
- `reports/phase-0/discovery-network-junit.xml`：新聞索引實際網路契約 1 failed（HTTP 429），沒有宣稱真實搜尋驗收通過。Review Bundle 的 research-manifest.json 明列 FAILED，合成搜尋紀錄另列，沒有混成真實證據。
- `reports/phase-0/us-price-network-junit.xml`：NVDA／AMD 次級行情來源的實際契約測試 2 passed；沒有保存價格、原始回應或雜湊，亦未啟用權限。
- `reports/phase-0/network-junit.xml`：真實台股網路契約（報價 2 項、損益表 2 項通過）；SEC 缺設定時保留 skipped（2 項）。
- `reports/phase-0/source-smoke.json`：2026-11-15 台北時間實際擷取紀錄，四個台股端點／標的組合成功解析，並含損益表累計基礎的比值核對；只落盤中繼資料，不構成真實研究 E2E。
- `reports/codex-evaluation/llm-evaluation.json` 與 [互動驗證紀錄](codex-interactive-evaluation.md)：實際單批十例、原始提案／決策／PDF 雜湊與限制。確切模型 ID 與用量無法取得時保留 null。
- `sample-stock.pdf`／`sample-event.pdf`：均為明顯標記的 DEMO；真實報告尚未解鎖。
- 本輪 `demo` 四份合成報告及 Replay、`qa`、`bundle` 均成功；事件範例先經可重現排序，Codex 已逐頁檢視事件 PDF，維持兩頁且無溢位。Apple Container 映像重建成功，Docker 引擎未實測。Fable 起草本輪模組後再次遇用量上限，由 Codex 補完、修正並驗收。

Review Bundle 會驗證本機互動紀錄的成品雜湊。若只剩版本化摘要，會明示原始成品不在本機；若沒有實際紀錄，才標 NOT_RUN。不在產生 Bundle 時冒充重新呼叫模型。

台灣財報增量（2026-11-15）由本機 Claude Code 實作並在 Apple Container 執行離線回歸、網路契約與 Smoke；財報的 Bundle／prepare 路徑只用合成測資與虛構授權登錄檔驗證，沒有任何真實財報數值進入研究包。

主要 Provider 實作由本機 Claude Code 完成；Codex 執行獨立驗收並修正未完成部分。Claude Code 本次曾實際執行容器測試及台股 Smoke Test，之後碰到五小時用量上限，由 Codex 完成驗收。未採購 API、未在主機安裝第三方依賴、未變更登入設定。

使用者已選擇維持來源 `unknown`、暫不啟用；以下權限前提保留，不推定為已取得簽認。

## 尚待完成的 Phase 0 條件

1. 依 [來源查核證據](taiwan-license-review-evidence.md) 核對已準備的有限端點／資料集範圍對應與顯名稿，依 AGENTS 的人工查證契約填入具名簽認與精確 scope；不得只因端點可連線就開啟權限。
2. 操作人明示 SEC 聯絡設定，再做 NVDA／AMD 真實申報來源測試；美股價格已加入次級來源的保守日線契約與兩項真實網路測試，仍缺授權及調整口徑核實，跨期漲跌幅維持 NA，詳見 [美股價格限制](us-price-source-notes.md)。仍需可靠事件等核心證據。台灣財報需另補前期可比數、現金流量、金額級距與申報時間的官方依據，並人工核對 2330／6488 的會計年度設定，才能支援成長率、PE 或自由現金流。
3. 完成 NVDA 與 2330 的真實研究 E2E、權限及版面驗收；資料不足時只能發布清楚標示的不足摘要。
4. 校準 Gate 核心門檻，核對自由文字推論；現有受控互動批次不證明所有新版本或投資預測正確。
5. 完成真實跨市場事件及候選排序驗證，再按市場與能力分別決定 GO／CONDITIONAL GO／NO-GO。

使用者已再次明示「維持關閉，繼續其他實作」，不再追問相同來源啟用。2330 查核包位於 `docs/phase-0/2330-source-activation.json`，`activation-check` 只讀檢查範圍與漂移，不能改寫登錄檔。來源啟用與真實報告仍未完成，非使用者目前待辦。

本輪結案範圍是 Phase 0 軟體與封閉來源下的驗收，不是正式 MVP 或投資效能驗證。Fable 仍受用量限制，本輪由 Codex 接手完成。剩餘真實研究條件維持 NO-GO；不以繼續增加合成測試或放寬權限假裝達成。當前設定下不要求使用者再操作或回覆「繼續」才能跑既有完整流程；可直接用 verify 與 doctor 核對。
