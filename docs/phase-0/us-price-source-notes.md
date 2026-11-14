# 美股日線可行性增量

此增量仍屬 Phase 0，不代表正式 MVP GO。Claude Code（Fable）完成初稿後觸及用量上限，後續由 Codex 補齊設定、測試及驗收。

`yahoo-chart-daily-close` 是次級資料供應者，不是交易所或公司申報。端點為 `https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=2y&interval=1d&events=split`。這是未確認正式支援與授權的端點，不能因為可連線就認定允許保存或分享。登錄狀態維持 unknown；所有保存、原始快取、雜湊及分享權限均不啟用。

NVDA／AMD 的對應設定僅是助手維護的可行性設定，並非人工身份簽認。解析器比對回應的 symbol、currency、exchangeName、instrumentType、dataGranularity 及名稱；不猜代號，不切換供應者，也不以合成資料補真實資料。

來源未宣告發布時間，所有證據採擷取後 observed_availability，不能支援早於擷取的研究截止。交易日與公開時間分開；以美東標準時間日期結束作為保守邊界，排除尚未結束的日期，不使用盤中最後價格。日線最多 800 列、800 日，最新完整交易日不得過期超過 10 日。

本端點 close 的分割／股利調整口徑尚未完成文件核實，因此固定 unknown。即使有歷史價點，真實資料的 3／6／12 個月價格變動仍為 NA。計算器僅對同一快照、同一來源、同幣別且明確 split_adjusted 的序列計算；含股利調整、未調整或未知口徑均不計算。合成測試可以明示調整基礎，不能稱為真實來源驗證。

尚待完成：正式來源文件與權限查核、可公開驗證的調整口徑、人工身份核對。此增量不解決事件候選探索與排序，亦不啟用真實研究輸出。

2026-11-15 在 Apple Container 明示執行兩項真實網路契約測試，NVDA／AMD 均通過身份與序列解析檢查；只保存測試結果，不保存來源價格、回應或雜湊。這只證明當次端點契約可用，不構成授權或正式調整口徑驗證。紀錄為 `reports/phase-0/us-price-network-junit.xml`。
