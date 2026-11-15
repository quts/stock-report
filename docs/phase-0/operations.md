# 本機操作與驗收

所有操作經 `scripts/runtime.sh`，預設 Apple Container；Docker 維持相同映像與指令契約，但目前沒有 Docker 引擎實測。

```sh
scripts/runtime.sh build
scripts/runtime.sh verify
scripts/runtime.sh cli doctor
```

`verify` 在單一容器依序執行完整離線測試、四份合成報告及 Replay、PDF 圖片預覽、Review Bundle；任一步失敗即停止並記錄失敗。最終 `reports/phase-0/acceptance.json` 保存各步退出碼、版本與操作狀態。它不呼叫模型、不抓資料、不改來源權限；不能宣稱是新一批模型評估或真實研究 E2E。

`doctor` 是唯讀診斷，顯示套件與程式版本、來源是否封閉、SEC 聯絡設定是否存在（只輸出布林值）、已記錄的離線／網路測試結果。讀取舊測試成品不代表重新執行或證明新版已測試。它不讀登入資料、不連線，也不修改登錄檔。

## 新聞服務的明確選擇

原有 `--source gdelt-doc-v2` 使用新聞索引；其網路測試回傳 HTTP 429，紀錄保留為 FAILED。新增 `--source nvidia-press-rss` 使用 [NVIDIA 投資人 RSS 頁面](https://investor.nvidia.com/investor-resources/rss/default.aspx) 連結的固定官方新聞稿 Feed：`https://nvidianews.nvidia.com/cats/press_release.xml`。2026-11-15 的實際 RSS 契約測試通過，紀錄在 `reports/phase-0/rss-network-junit.xml`。不同來源不會自動互換，RSS 通過不會抹除 GDELT 的失敗。

```sh
scripts/runtime.sh cli news-discover --source nvidia-press-rss --request reports/news/request.json --allow-network --output reports/news/rss-inspection.json
```

Request 的截止時間須有效且新聞窗口在最近 60 天內。個股入口只支援此 Feed 的 NVDA；事件入口以標題逐詞匹配。Feed 最多解析 200 項、輸出最多 100 項、2 MB、20 秒，不下載文章或跟隨轉址，拒絕 DTD／實體宣告。發布日來自來源 pubDate，不把它當成內容已核實的證明。Feed 歷史有限，空結果不代表沒有事件。

真實結果仍只保存契約、數量與狀態。官方提供 RSS 不代表本專案已取得新聞內容保存／研究權限；本輪不啟用任何來源，不產生真實投資結論，不要求使用者重複核對來源。

## 評等統計

Quant 2 對機構名稱做 Unicode、大小寫與空白正規化；可設定映射採相同機構鍵。標準五級原詞仍保存，明確的機構別映射才可將 Outperform 等詞映射到三類，未知詞保留 Unclassified。

同機構以最新有效發布時點計一次；同時點原詞相互矛盾時，保留全部證據與原詞、列 Unclassified 並揭露，不依 Evidence ID 任選 Buy 或 Sell。目標價衝突、幣別不明或不一致時不輸出價格。機構數、有效消息數與最新評等的分析師紀錄數分開計算；分析師以「機構＋正規化姓名」識別，不跨機構合併同名者。過期、未知權限、未核實、截止日後資料不增加樣本分母。映射衝突或非法輸出類別會直接拒絕。

此增量不新增評等付費資料源、不保證全市場涵蓋，缺評等仍可產生標示不足的個人 PDF。Quant 版本升為 2，舊版研究需保留原程式重播或重新 prepare，不能篡改快照雜湊。
