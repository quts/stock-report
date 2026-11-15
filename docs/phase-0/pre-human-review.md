# 人工核對前的來源查證結果

查核日期：2026-11-15。查核者：Codex 助手，不是人工簽認。Fable 本輪回報用量上限，由 Codex 接手。所有來源權限保持關閉；本文件不是要求使用者立即核對或啟用。

## 收斂結果

開 PR 前的本機審查另修正舊 probe 的未知權限留存及 Quant 入口的授權／截止時點檢查；發現、修正與人工核對順序見 [本機 Code Review](local-code-review.md)。人工核對僅涵蓋有限來源，啟用後仍須另做真實資料驗收。

最終交付包稽核補正：`doctor` 與 Review Bundle 共用測試狀態判讀，零測試為 NOT_RUN、全部跳過為 SKIPPED、部分跳過為 PARTIAL，不能再把未執行契約標成 PASSED。交付包同時收錄本文件、財報比對、有限啟用參考及現行互動驗證摘要，manifest 明列現行驗證入口。

最終功能稽核另補了目前版本的實際 Codex 十例互動批次，並修正其揭露的 Prompt 版本契約不一致；結果見 [新版互動驗證](codex-interactive-evaluation.md)。來源查核不再是唯一核對項目，交換 Schema、Gate、成品雜湊及現行 PDF 也已納入前置驗證。

最終一致性稽核已同步授權登錄檔 0.4.1 與 2330 查核包基準：更新已完成的財報對應與 SEC 官方存取引用，移除過時的「尚未查閱」待辦，未修改任何 scope 或人工簽認。前置查證、不可由人工猜測的上游限制及日後才能執行的真實驗收分開記錄。

| 項目 | 已完成的前置查證 | 處置 |
| --- | --- | --- |
| 2330 身份與最新收盤 | 官方公司身份參考、政府資料集 11549 與條款、CSV／JSON 目標欄位比對、日期上界與有限顯名稿均有紀錄 | 可保留為有限人工核對包，但使用者已決定不啟用，不再追問 |
| 台股財報 CSV／JSON 對應 | 本輪直接從資料集 91998／94853 指定 CSV 與專案 JSON 端點，在記憶體比對名稱、年度、季別、營收、歸屬母公司淨利與 EPS；只保存布林結果 | 詳見 financial-source-binding.json；比對一致僅支持當次目標欄位，不能推及全部資料或授權 |
| 2330 會計年度與單位 | 已讀公司 2025 年合併財報，年度欄為 12 月 31 日；綜合損益表表頭明列新台幣千元、EPS 除外 | 支持該份財報的期間與單位，不能把這份 PDF 的單位或發布日自動套到最新 API |
| 最新台股 API 金額級距及累計基礎 | 政府資料集欄位與下載 CSV 標頭沒有明列金額級距；累計基礎仍只有先前月營收交叉比對 | 不把未查得的定義交給人猜；保留 undeclared、禁止絕對值跨來源比較及不可比成長率；財報來源不建議啟用 |
| Yahoo 調整價格 | 找到官方 adjusted close 說明；直接開啟 Help 回覆 429，搜尋索引仍可讀到官方摘要。Yahoo 條款頁直接開啟回覆 999 | 可確認官方一般 adjusted close 描述包含分割與股利；不能證明未文件化 chart JSON close 欄位是 split-only。跨期價格變動維持 NA，此端點不建議啟用 |
| SEC NVDA／AMD | 官方 API 與開發文件可查，既有 CIK／欄位契約與離線測試完成，速率限制可查 | 缺操作人明示聯絡設定時不實際呼叫；不能取用登入 email。此為外部設定限制，不是讓人工猜值的待辦 |
| GDELT | 已實際測到 HTTP 429，未假裝通過或反覆繞過限制 | 從目前建議使用路徑排除；保留失敗紀錄。它不是所有其他功能必須等待的前置條件 |
| NVIDIA 官方 RSS | 官方 IR 頁面直接提供 Feed，實際契約及 CLI 探查成功，XML、日期與不留存路徑已測試 | 作為明確可選的新聞探查替代，非隱藏 fallback；只保存中繼資料，不推定新聞原文可留存或成為研究證據 |

## 官方依據

- [上市一般業損益表 91998](https://data.gov.tw/dataset/91998)、[上櫃一般業損益表 94853](https://data.gov.tw/dataset/94853)：均標示政府資料開放授權條款第 1 版、每三個月更新，並指定 CSV 與 OpenAPI 文件。欄位清單只對基本 EPS 標示「元」，不能據此推定營收金額級距。
- CSV：`https://mopsfin.twse.com.tw/opendata/t187ap06_L_ci.csv`、`https://mopsfin.twse.com.tw/opendata/t187ap06_O_ci.csv`。可重跑 `scripts/runtime.sh review-sources --allow-network --output reports/financial-source-binding.json`；輸出不含來源數值與回應雜湊。
- [台積電官方 2025 合併財報](https://investor.tsmc.com/sites/ir/financial-report/2025/TSMC%202025Q4%20Consolidated%20Financial%20Statements_E_0.pdf)：PDF 第 7–9 頁的年度欄及單位表頭；附註 2 為董事會核准日期，不能冒充任何 API 的精確公告時間。只記錄結構與出處，未保存財務數值或 PDF 副本。
- [Yahoo adjusted close 官方說明](https://help.yahoo.com/kb/SLN28256.html)、[Yahoo 條款](https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html)：記錄的是查核與存取限制，不是授權已確認。一般頁面說明與實際未文件化端點契約分開判定。
- [SEC API](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)、[開發者規範](https://www.sec.gov/about/developer-resources)：每位使用者合計最多每秒十次請求、需分類識別自動工具；本專案現行頻率更保守。
- [NVIDIA 官方 RSS 入口](https://investor.nvidia.com/investor-resources/rss/default.aspx)：對應現行固定 Feed；其實測結果與 GDELT 失敗分開保留於驗收紀錄。

## 不交給人工解決的事項

不要求人工為 Yahoo 的未文件化欄位猜調整口徑，不要求人工替 API 編造金額單位、累計定義或精確發布時間，不要求人工修復服務端限流。這些來源／欄位保持停用或省略；若未來取得新的官方契約，先由程式開發端補查及測試，才更新核對包。

「已完成前置調查」不代表所有未知均能變成已知，也不代表完整 MVP 或真實研究已驗收。可供核對的有限範圍與不建議使用的範圍已分開；使用者目前仍選擇關閉，不在本輪再次詢問。
