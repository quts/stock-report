# Provider 可行性紀錄

本文件記錄 Phase 0 實際探查邊界；精確時間、回應 Hash、取得值與錯誤在 `reports/phase-0/provider-probes.json`。此檔與其他原始稽核資料均預設不納入 Git。端點能存取不代表具有分享權限。

| 候選來源 | 端點／結果 | 涵蓋與缺失 | 授權／成本判斷 |
| --- | --- | --- | --- |
| SEC NVDA | `data.sec.gov/api/xbrl/companyfacts/CIK0001045810.json`；本次未發出請求 | 未配置來源要求的聯絡 User-Agent；不取用登入信箱 | 無付費 API；內部與分享條款尚待查核 |
| SEC AMD | `data.sec.gov/api/xbrl/companyfacts/CIK0000002488.json`；本次未發出請求 | 同上 | LICENSE_UNKNOWN |
| TWSE | `openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL`；實際取得 2330 紀錄 | JSON 含日期、代號、公司名、OHLC、量值；未驗證歷史深度／財報／發布時間契約 | 免費存取實測；LICENSE_UNKNOWN，禁止進入分享 PDF |
| TPEx | `www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes`；實際取得 6488 紀錄 | JSON 含日期、公司名、收盤、量值；未驗證歷史深度與股類、幣別 | 免費存取實測；LICENSE_UNKNOWN，禁止進入分享 PDF |
| 公司 IR／台灣財報公告 | 尚未實測 | 須核對期別、公告時間與資料使用範圍 | 未確認 |
| 美國歷史價格 | 尚未選定 Provider | 調整價格、額度、分享權限均未確認 | 未確認 |
| 全球新聞／RSS／GDELT | 尚未實測 | 不能推論原始全文可再散布 | 未確認 |
| 逐機構評等 | 未找到已驗證的免費、可分享 Adapter | 不使用三大法人交易冒充評等 | 資料不足仍可產 Mock PDF |

來源條款候選連結（**尚未完成內容核對，不是授權證明**）：

- SEC 開發資源：<https://www.sec.gov/about/developer-resources>
- TWSE 使用條款：<https://www.twse.com.tw/zh/terms/terms.html>
- TPEx 使用條款：<https://www.tpex.org.tw/zh-tw/terms.html>

HTTP 探查保守預設每秒最多一次、有限重試及逾時；這是工具的限制，不是來源已承諾的正式額度。SEC 呼叫需操作人自行提供來源接受的 User-Agent；未配置便明確阻擋。官方端點的公開日期不得由擷取時間推定；目前探查資料不進入歷史時點研究。

每項來源下一步需補內部使用權、PDF 摘要／數字再利用權、引用要求、查證日期、有效條款連結、額度／Key 需求及穩定性測試。現階段所有真實來源 `shareable_in_pdf=false`，不產真實分享版 PDF。
