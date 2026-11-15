# 本機操作與驗收

所有操作經 `scripts/runtime.sh`，預設 Apple Container；Docker 維持相同映像與指令契約，但目前沒有 Docker 引擎實測。

```sh
scripts/runtime.sh build
scripts/runtime.sh verify
scripts/runtime.sh cli doctor
```

`verify` 在單一容器依序執行完整離線測試、四份合成報告及 Replay、PDF 圖片預覽、Review Bundle；任一步失敗即停止並記錄失敗。最終 `reports/phase-0/acceptance.json` 保存各步退出碼、版本與操作狀態。它不呼叫模型、不抓資料、不改來源權限；不能宣稱是新一批模型評估或真實研究 E2E。

`doctor` 是唯讀診斷，顯示套件與程式版本、來源是否封閉、SEC 聯絡設定是否存在（只輸出布林值）、已記錄的離線／網路測試結果。讀取舊測試成品不代表重新執行或證明新版已測試。它不讀登入資料、不連線，也不修改登錄檔。`enabled_source_review_basis` 列出每個已啟用來源的查核依據（`human_attestation` 或 `assistant_source_review`）。

## 來源查核與啟用

2026-11-15 起，使用者授權由 AI 查核官方來源、身份、時間與使用條款；證據充分才啟用，查不清楚繼續封鎖，範圍限最低合法、免費、僅自用不散布。

1. 查核者在候選登錄檔（不是 `config/source-licenses.json` 本身）把來源設為 `confirmed`、`scope` 只開 `internal_use_allowed`，並填寫 `source_review`：`reviewed_by=assistant`、`reviewer`、`reviewed_at`、`authorization`（使用者授權的出處）、`reviewed_endpoint_scope`（須等於 `endpoint_scope`）、`verdict=sufficient`、`findings`、空的 `unresolved`。`findings` 必須涵蓋 `official_source`、`identity`、`publication_time`、`terms_of_use`、`free_access`，各有官方 https 網址、查核時間、逐字引用與結論；條款面向須引用登錄的 `terms_url`，且同一網址須有 `terms_evidence` 逐字引用。
2. 不得填 `attested_by`／`attested_at`，不得新增 `checked_by=human` 的紀錄；既有助理查核紀錄不得刪改。有未決問題時記為 `verdict=insufficient` 並維持 `unknown`。
3. 執行唯讀比對；`candidate_matches_package` 且 `candidate_review_basis` 為預期依據後，才套用到登錄檔並提升 `registry_version`：

```sh
scripts/runtime.sh cli activation-check --package docs/phase-0/2330-source-activation.json \
  --candidate-registry reports/candidate-registry.json
```

4. 啟用後重新 `collect --allow-network`；封閉期間的採集不沿用新權限。`bundle` 報告的每筆採集會標示 `source_reviewed` 與 `review_basis`。

`activation-check` 支援兩種標的。臺灣標的（`code`＋`instrument_config`）預設以 `config/tw-instruments.json` 核對。SEC 標的（`cik`＋`expected_entity_name_contains`＋`entity_config`）只以查核包指定的 `config/sec-entities.json` 核對 CIK、名稱綁定、交易市場、幣別與股份類別；項目須附 `sec_binding`（taxonomy、annual_forms、期間天數、各 role 的 concepts／units／unit／metric_keys／required，以及依 CIK 展開的 `request_urls`），`field_binding` 為 `cik`／`entityName`，全部逐字比對來源契約。設定檔錯誤、CIK／市場／單位／concept／端點不符、或來源與標的種類不相容一律 `blocked`。`--target-config` 是 `--instruments` 的別名。啟用仍以來源（端點範本）為單位，不是單一 CIK。

舊查核包的 `awaiting_human_attestation`／`ready_for_human_review`／`human_checklist` 仍可讀取，視同「等待來源查核」；新查核包可用 `awaiting_source_review`／`ready_for_source_review`／`review_checklist`。查核包基準版本與目前登錄檔不同時，`activation-check` 會回報漂移並封鎖，需更新查核包。不帶候選檔時，若登錄檔已對查核包建議的來源記錄查核依據，狀態為 `registry_already_reviewed` 並列出依據，否則為 `awaiting_source_review`。此檢查只驗證範圍與紀錄結構，不能證明引用內容與官方頁面相符。回應雜湊、原始回應留存仍需人工簽認。

## 新聞服務的明確選擇

原有 `--source gdelt-doc-v2` 使用新聞索引；其網路測試回傳 HTTP 429，紀錄保留為 FAILED。新增 `--source nvidia-press-rss` 使用 [NVIDIA 投資人 RSS 頁面](https://investor.nvidia.com/investor-resources/rss/default.aspx) 連結的固定官方新聞稿 Feed：`https://nvidianews.nvidia.com/cats/press_release.xml`。2026-11-15 的實際 RSS 契約測試通過，紀錄在 `reports/phase-0/rss-network-junit.xml`。不同來源不會自動互換，RSS 通過不會抹除 GDELT 的失敗。

```sh
scripts/runtime.sh cli news-discover --source nvidia-press-rss --request reports/news/request.json --allow-network --output reports/news/rss-inspection.json
```

Request 的截止時間須有效且新聞窗口在最近 60 天內。個股入口只支援此 Feed 的 NVDA；事件入口以標題逐詞匹配。Feed 最多解析 200 項、輸出最多 100 項、2 MB、20 秒，不下載文章或跟隨轉址，拒絕 DTD／實體宣告。發布日來自來源 pubDate，不把它當成內容已核實的證明。Feed 歷史有限，空結果不代表沒有事件。

真實結果仍只保存契約、數量與狀態。官方提供 RSS 不代表本專案已取得新聞內容保存／研究權限；新聞來源是否啟用同樣依登錄檔的查核依據；未查核前不保存內容、不產生真實投資結論。

## 評等統計

Quant 2 對機構名稱做 Unicode、大小寫與空白正規化；可設定映射採相同機構鍵。標準五級原詞仍保存，明確的機構別映射才可將 Outperform 等詞映射到三類，未知詞保留 Unclassified。

同機構以最新有效發布時點計一次；同時點原詞相互矛盾時，保留全部證據與原詞、列 Unclassified 並揭露，不依 Evidence ID 任選 Buy 或 Sell。目標價衝突、幣別不明或不一致時不輸出價格。機構數、有效消息數與最新評等的分析師紀錄數分開計算；分析師以「機構＋正規化姓名」識別，不跨機構合併同名者。過期、未知權限、未核實、截止日後資料不增加樣本分母。映射衝突或非法輸出類別會直接拒絕。

此增量不新增評等付費資料源、不保證全市場涵蓋，缺評等仍可產生標示不足的個人 PDF。Quant 版本升為 2，舊版研究需保留原程式重播或重新 prepare，不能篡改快照雜湊。
