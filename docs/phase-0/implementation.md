# Phase 0 實作與容器操作

## 環境與版本控制

實作分支 `codex/phase-0`。使用 Python 3.11、Pydantic v2、SQLite、Jinja2、WeasyPrint、pytest；Python 與 CJK 字型均安裝在容器內。`Dockerfile` 同時適用 Apple Container、Docker；`scripts/runtime.sh` 預設使用 Apple Container，`CONTAINER_ENGINE=docker` 切換 Docker。Docker 尚未在此環境實測，不宣稱雙引擎驗證均已通過。

```sh
./scripts/runtime.sh build
./scripts/runtime.sh test -q --junitxml=reports/phase-0/junit.xml
./scripts/runtime.sh demo
./scripts/runtime.sh qa
./scripts/runtime.sh bundle
```

建置需要網路取得基礎映像及依賴；一般測試與 Mock 管線不呼叫市場資料或模型 API。依賴直接版本固定在 requirements.txt，間接依賴尚未完整鎖定；Manifest 記錄程式與模板雜湊，但不主張跨任意映像可逐位元重製。

## 固定執行契約

`prepare --request JSON --provider mock|import [--evidence-bundle JSON] --output reports` 驗證 Request，產生 UUID 研究目錄、Evidence SQLite、JSON、提案 Schema、Prompt、輸入雜湊。匯入數值會重新計算，不能信任外部提供的 Quant 結果；不得把合成資料標為真實資料。

`analyze --run DIRECTORY` 只輸出受控交換檔；Codex 在互動環境依證據產生提案。此命令不使用付費 API、不假裝已呼叫模型。`--synthetic` 僅允許 Mock，產生清楚標示為合成的測試提案。此版本沒有任意自然語言 shell 執行功能。

`finalize --run DIRECTORY --proposal JSON [--rating-map config/ratings.json]` 檢查輸入 Hash、提案 Schema、每家公司每期限唯一提案、Policy、分享過濾及 PDF 頁數。輸出 PDF 前先寫暫存檔；失敗不宣稱完成。已完成的研究目錄不可直接覆寫。

`replay --run DIRECTORY --output reports` 驗證來源檔案 Hash 與程式／模板／Policy／映射版本，在新 UUID 目錄重播，要求決策結果雜湊一致。此版本不支援從較舊程式自動重建舊環境；版本不符便拒絕。Fresh Run 必須重新 prepare。

容器 `/work` 綁定主機 repository；例如 `/work/reports/abc/report.pdf` 的主機路徑為 `<repository>/reports/abc/report.pdf`。

## 研究模型與計算

身份包括 ticker、exchange、currency、share_class；時間使用時區感知 datetime。區分事件、公開、擷取、研究截止及授權查證時間。每筆 Evidence 保留來源 URL、來源 ID、發布者、原始主張、期別、原始評等及權限。

`metric_key` 為正規化的計算角色，與來源本身的 `source_record_id` 分開。現階段支援相鄰 FY 營收／EPS 成長、同期期別淨利率、營業現金流減正規化資本支出的自由現金流、正 TTM EPS 本益比。不同幣別／單位、不可比期別、非正分母或缺值不計算；缺值不是零。TTM 成長、P/S、EV/EBITDA、負債指標、調整後歷史價格及同業比較未實作，不以替代數字冒充。

評等映射經版本管理，保留原詞彙與機構、分析師、訊息數的不同分母。同機構取截止日前最新有效紀錄；有效期限未知不當成仍有效，未定義詞彙列 Unclassified。樣本不足時仍輸出 PDF，顯示「投顧共識資料不足」。

## Gate 與發布邊界

預設零次自動修正：不循環呼叫模型；相關期限違規直接降級 insufficient 並保留規則。一次修正是規格初始建議而非必須開啟的能力。`REVISE` 保留在狀態 Schema，目前沒有自動修正流程。

Hard Gate 包括未知 Evidence ID、錯誤標的、截止日後資料、未核實／矛盾主張、禁止分享／權限未知、Quant 重算不符、核心證據不足、未解決分歧及無法核實的事件關聯。錯誤主要限於公司／期限；身份或 PDF 核心流程不成立才不產報告。所有期限方向被拒仍可生成清楚標記的資料不足摘要。

Phase 0 核心門檻為兩個不同來源紀錄及反轉條件，**尚未校準**。相同發布者另附單來源警告，不稱為獨立交叉驗證。成長趨勢與 AI 方向相反須具體解釋，否則降級；不直接平均為 Neutral。Hypothetical 關聯不發布方向性結論。

Gate 能驗證結構、時點、數值、引用與輸入權限，不能證明自由文字推論與來源語意一致，也不能證明預測正確。真實輸入中的 verified／confirmed 標記須由人工來源查核支持；全自動真實研究尚未驗收。

PDF 只接收 Gate 結果和可分享證據的投影，模板自動跳脫 HTML，禁止 Renderer 讀取本機／遠端 URL 資源。超過兩頁直接失敗，不縮成極小字體。頁尾採簡潔頁碼；使用限制與風險揭露放在正文，避免頁尾渲染影響必要揭露。長敘述用明示摘要，完整本機 JSON 保留。報告為 A4，股／事件兩版，分列 3–6、6–12 個月。

## 儲存、授權與安全

`reports/<run_id>/` 包含 report.pdf、research.json、evidence.json、evidence.sqlite、decision.json、ai_proposal.json、rating_mapping.json、research_input.json、manifest.json。只有 PDF 被列為通過發布過濾的檔案；原始／稽核 JSON 與 SQLite 是本機研究資料，不能視為自動具分享權。預設不保存網頁全文或第三方研究報告。

HTTPClient 提供有限 Retry、速率限制與可選 TTL Cache；原始回應 Cache 預設停用，僅在來源內部保存授權確認後由 Adapter 顯式啟用。探查只保存目標欄位及回應 Hash，不保存全文。CLI 不執行來源文字、不組 shell、不讀取登入憑證。User-Agent 聯絡資訊由操作人自行配置，不能從登入資料取得。

## 尚未驗收的必要項目

真實台美財報／行情正規化與分享條款、逐機構評等來源、跨市場真實事件探索排序、Codex 十例互動提案驗證、真實股／事件兩頁報告、校準 Gate 門檻。這些狀態必須留在 Review，不能用 Mock 測試通過取代。
