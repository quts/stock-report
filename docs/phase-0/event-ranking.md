# 事件卷宗與候選排序

2026-11-15 增量：由 Claude Code Fable 起草，觸及用量上限後由 Codex 完成驗收。這是有界候選集合的證據排序，並非自動新聞搜尋或全市場發掘。

## 執行

所有命令透過 Apple Container；Docker 使用相同 runtime 契約。明示合成範例：

```sh
scripts/runtime.sh cli event-rank --dossier examples/events/dossier-synthetic.json --output reports/events
scripts/runtime.sh demo
scripts/runtime.sh qa
```

`event-rank` 輸出 `event-ranking.json`；成功時另有 `event-bundle.json` 與 `request.json`。失敗回傳非零狀態且不留下可用證據包。結果含具體路徑與 `next_provider`：真實卷宗只能走 `prepare --provider import --evidence-bundle`；合成卷宗必須明示 `--provider synthetic-event`，不能混入真實匯入流程。

準備後依原契約讀取 research_input.json、context.json、proposal.schema.json 及 analysis-instructions.txt，再提供非合成的模型提案、finalize。合成範例只驗證程式，不代表模型或真實事件驗收。

## 規則與限制

- 支援清單在 `config/event-universe.json`，目前為 2330.TW、NVDA、AMD、6488.TWO；清單是設定，不是身份證據。身份證據與市場、幣別、股類必須核對。未出現在卷宗的公司標為 not_in_dossier，不推論沒有關聯。
- 事件需有可用官方來源，或至少兩個獨立次級發布者／來源。獨立性使用卷宗標示的 origin_id、來源 URL、發布者及紀錄鍵保守去重；不能自動證明媒體沒有互相轉載。
- 事件證據必須在截止日前，且發布時間落在 Request 的 lookback_days 新聞窗口。傳聞不能視為已核實；提案存在不代表已生效。
- 直接關聯需官方點名或公司揭露；揭露的業務連結為間接；只有產業同類或推論為假設。卷宗自行宣告的關聯類別不能提升證據推導結果。
- 排序依直接／間接／假設、獨立官方來源數、獨立來源數、公司代號排序，最多選 Request.max_companies（不超過五家）。這是關聯證據強度，不是受益程度、投資分數或展望。
- 未核實、錯標的、截止日後、未知／禁止內部使用及未納入人工查核的證據均排除。關閉的已登錄端點不能透過刪除 license_source_id 或填入其他來源代號繞過。
- 真實卷宗須記錄人工查核者、時間與實際核對的證據 ID；程式不能證明簽認真假，也不能取代核對來源內容。陌生來源的人工匯入仍須依原研究契約先查核身份、時間與使用權限。
- 排序紀錄只保留 ID、理由及中繼資料；不複製來源主張或數值。成功的研究證據包仍屬本機個人資料，不對外分享。

## 驗收範圍

離線測試涵蓋順序重現、輸入順序變更、同源去重、身份錯配、錯誤引用、權限與截止時間、未知端點繞過、新聞窗口、假設降級、公司數上限，以及 CLI → prepare → finalize 的合成 PDF。

`demo` 的事件範例已經過排序後再產生報告；排序紀錄在 `reports/demo/event-ranking.json`。真實來源維持關閉，因此尚未有真實事件探索、真實卷宗或真實研究 E2E 驗收，不能以合成測試宣稱完成。
