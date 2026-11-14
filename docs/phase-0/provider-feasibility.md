# Provider 可行性與來源授權紀錄

## 已實測的來源契約

2026-11-15（Asia/Taipei）已在 Apple Container 執行真實來源 Smoke Test。精確擷取時間與欄位對應見 `reports/phase-0/source-smoke.json`；測試不使用 Mock 退路，也不把端點可得性當成授權。

| 來源／標的 | 實際狀態 | 欄位與時間基準 | 權限 |
| --- | --- | --- | --- |
| TWSE STOCK_DAY_ALL／2330.TW | 成功解析真實回應 | Code、Name、ClosingPrice、Date；實測日期為民國格式 | unknown，僅保存欄位契約中繼資料 |
| TPEx tpex_mainboard_daily_close_quotes／6488.TWO | 成功解析真實回應 | SecuritiesCompanyCode、CompanyName、Close、Date；實測日期為民國格式 | unknown，僅保存欄位契約中繼資料 |
| TWSE t187ap06_L_ci／2330.TW | 成功解析真實回應 | 公司代號、公司名稱、年度、季別、出表日期、營業收入、淨利（淨損）歸屬於母公司業主、基本每股盈餘（元）；年初至今累計，出表日期為民國格式 | unknown，僅保存欄位契約中繼資料 |
| TPEx mopsfin_t187ap06_O_ci／6488.TWO | 成功解析真實回應 | SecuritiesCompanyCode、CompanyName、Year、Season、Date 及相同財務科目欄位；年初至今累計 | unknown，僅保存欄位契約中繼資料 |
| SEC companyconcept／NVDA、AMD | 離線契約通過，網路跳過 | 年度 10-K 的營收、淨利、稀釋 EPS、營業現金流、資本支出；CIK 與名稱雙重檢查 | unknown；缺操作人明示的 SEC 聯絡設定 |

數值、公司原名與來源紀錄 ID 未在未知內部保存權限下落盤。檢視紀錄只有欄位名稱、形狀、數量、擷取時間、公開時間基準、權限與 blockers；不能當成可供研究的 collection.json。

## 官方授權證據與待核對事項

具體官方頁面、資料集指定資源及授權條款已由 Codex 查核，見 [台股授權查核證據](taiwan-license-review-evidence.md)。登錄檔記錄 `checked_by=assistant`；這不是人工簽認。另已在記憶體內比對政府資料集指定 CSV 與實際 JSON：2330／6488 的名稱及收盤欄位一致，見 [比對紀錄](taiwan-source-binding.json)；結果不擴及其他欄位、標的或歷史資料。

- [11549：盤後資訊 > 個股日成交資訊](https://data.gov.tw/dataset/11549) 列明政府資料開放授權條款第1版，指定 TWSE CSV 並連到 OpenAPI 文件。
- [11371：上櫃股票收盤行情](https://data.gov.tw/dataset/11371) 同樣列明該授權，指定 TPEx CSV 並連到 OpenAPI 文件。
- [政府資料開放授權條款](https://data.gov.tw/license) 包含利用權利與顯名義務；不能省略來源標示或宣稱政府認可研究方向。

`config/source-licenses.json`（版本 0.2.0）五個來源皆為 unknown：沒有內部保存、PDF 分享、原始回應或回應雜湊的已確認權限。confirmed 需要依現行 AGENTS 的人工查證契約，保留具體條款、日期、具名 attestation 及 scope。候選來源不因網站屬於同一交易所而共用權限。

採集時與使用時的權限取交集。實際請求端點、載入的採集端點及逐筆來源 URL 都須位於登錄的範圍，授權改動或撤回會阻擋原本不再適用的內容。

## 時間、身份與數值限制

- 來源未宣告公開時間時，採用回應完成後的 `observed_availability` 上界；不是精確發布時間，不能回填到較早的歷史研究。
- 台股同日資料的時間上界不晚於實際觀測時間，未來交易日仍拒絕；支援的民國／西元格式明列於來源契約。
- SEC 申報日期以保守日終上界處理。只使用截止前可得的已採集版本；較晚修訂可能使舊期別被排除，不保證完整歷史重建或回補早期版本。
- 台股身份／股類使用 `config/tw-instruments.json` 的明示 POC 設定（2330、6488），不是官方股類資料流；四位數代號本身不足以證明普通股。其他標的需增補證據與設定。
- `source_record_id` 保留來源記錄身份，與計算角色 `metric_key` 分離；原始期間、單位及申報日另存。
- 台灣損益表來源沒有宣告期間起訖、累計基礎、金額級距與申報時間；處理方式、累計基礎的實測核對與省略的指標見 [財報來源查核紀錄](taiwan-financial-source-notes.md)。
- 財務證據保存期間起訖與基礎；年增率只在兩個相鄰的完整會計年度之間計算，同期比率要求起訖相同。
- SEC 年度 EPS 不冒充 TTM EPS；缺季度序列時不計算 PE。不可比年度、幣別、單位與缺值不以猜測補足。
- 單筆可合法使用的收盤價可進入不足摘要，不能單獨支持偏多、偏空或中性方向。機構評等、歷史調整價、同業比較仍有缺口；台灣財報只有最新一期損益表摘要。

SEC 聯絡資訊只能由操作人以 `--sec-contact`／`STOCK_REPORT_SEC_CONTACT` 明示提供，不會從 Claude／Codex 登入資料取得，亦不保存到採集紀錄。
