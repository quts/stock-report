# 有界新聞索引與公司線索

另有明確可選的 NVIDIA 官方新聞稿 RSS（`--source nvidia-press-rss`），其實際網路契約已通過；這不會抹除 GDELT 的 HTTP 429。使用方式與兩種來源差異見 [本機操作與驗收](operations.md)。

2026-11-15：Codex 曾交由本機 Claude Code Fable 接手，但 Fable 立即回報五小時用量上限，因此本增量由 Codex 實作與驗收。

## 使用方式

```sh
# 明示合成資料：可檢視完整標題、連結與待查公司提及。
scripts/runtime.sh cli news-discover --request examples/event.json --fixture examples/news/index-synthetic.json --output reports/news/synthetic.json

# 真實探查須自行準備受控 Request，截止時間與新聞窗口須在最近 60 天內。
scripts/runtime.sh cli news-discover --request reports/news/request.json --allow-network --max-results 25 --output reports/news/inspection.json
```

也支援 stock Request，例如 NVDA 或 2330.TW，使用設定中的明確公司名稱查詢。使用 `scripts/runtime.sh demo` 可同時產出合成搜尋紀錄 `reports/demo/news-discovery.json` 與獨立的事件排序紀錄。兩者不是同一驗證層級：搜尋提及不能自行轉為已核實卷宗。

## 實際行為

- 固定呼叫 GDELT DOC 2.0 的 HTTPS ArtList JSON 端點；依[官方 API 文件](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/)使用 query、startdatetime、enddatetime、maxrecords 及 DateDesc。查詢詞只接受受控 Request，拆成最多 12 個有長度上限的詞後逐一引用，來源文字不會進 shell，也不能注入搜尋操作符。
- 一次查詢、20 秒逾時、2 MB 回應上限、1–100 筆結果、不追隨重新導向、不下載文章、不自動重試。拒絕無時區、未來或超出本實作最近 60 天範圍的窗口，不默默改寫截止時間。官方文件的可查窗口較廣，本實作刻意較窄。
- 只以索引回傳的標題匹配設定 `config/discovery-aliases.json` 的四家公司；名稱需字界或明確中文名稱。裸數字及 AMD／NVDA 等裸代號不匹配；市場限定代號與 `$NVDA` 可作提及線索。別名同時指向多家公司時不替使用者選擇。
- 檢查日期格式與窗口、文章網址及網域一致性；網址只作資料解析，從不開啟。去除追蹤參數後去重；同網址不同標題或索引時間視為衝突，整組排除，不拼接成事實。
- seendate 僅標記為索引時間，不當成原文發布日或事件發生日。搜尋結果可能不完整，達筆數上限會標示 possibly_truncated；未命中不表示無關。
- 回傳內容是未核實線索，relationship=unassessed、verification_status=unverified；不產生 Evidence，也不能送入 prepare/import 或自動簽認。後續必須查原始主張、身份、時間、關聯與權限，再依事件卷宗契約排序。

## 權限與輸出

GDELT 端點尚未完成本專案的使用權限查核；沒有新增已啟用來源或 API Key。無論傳回哪些真實內容，網路模式只可輸出端點、契約版本、狀態、數量、排除理由及限制；不保存標題、文章網址、公司提及清單、原始回應或內容雜湊。合成 Fixture 的完整線索只供離線驗證，不能冒充真實來源。

API 可連線不代表新聞原文或索引內容可保存。本輪不改變使用者「維持來源關閉」的選擇，也不要求再次核對來源。失敗會以非零狀態與不含來源內容的錯誤類型回報，沒有 Mock fallback。

## 實際網路驗證

2026-11-15 明示執行 `scripts/runtime.sh test -q -m network tests/test_discovery.py`：服務回傳 HTTP 429，因此一項網路契約測試失敗，不能宣稱真實搜尋可用或已完成真實研究驗收。紀錄在 `reports/phase-0/discovery-network-junit.xml`，沒有保存來源內容；未以重複請求、隱藏備援來源或合成資料掩蓋失敗。
