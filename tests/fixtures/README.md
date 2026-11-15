# 離線契約測資來源說明

這些檔案是 **自行撰寫的形狀測資（shape fixtures）**，不是任何來源的實際回應擷取檔：

- TWSE／TPEx 的**欄位名稱與民國交易日格式已依實際探查樣本校正**
  （`reports/phase-0/provider-probes.json`：`Date=1151113`、`Code`／`Name`／`ClosingPrice`、
  `SecuritiesCompanyCode`／`CompanyName`／`Close`）。SEC 欄位契約仍未經實際回應核對。
- `twse_income_general.json`／`tpex_income_general.json` 的 33 個欄位名稱與民國年度／季別／出表日期形狀
  依 2026-11-15 實測回應形狀撰寫（見 `docs/phase-0/taiwan-financial-source-notes.md`）；公司列與所有數值皆為示意值。
- 檔案中的**數值一律為示意值**，不是任何來源的實際數字：授權登錄檔尚未確認內部留存權限，
  因此不得把真實回應數值寫進版本控制。
- 數值皆為示意值，刻意不等於任何真實市場或財報數字，不得用於任何研究結論。
- `licenses-attested.json` 的授權狀態與署名為 **虛構測試資料**，只用來驗證
  「已確認內部使用」程式路徑（`pdf_share_allowed` 一律為 false），不代表任何來源已授權內部使用；
  本專案報告僅供個人使用，不存在分享版。

採集流程會把 `--fixture` 標記為 `capture_mode=fixture`；`bundle` 預設拒絕這類採集，
只有離線測試明確傳入 `--allow-fixture-capture` 才會接受。
