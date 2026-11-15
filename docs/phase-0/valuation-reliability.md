# 估值可靠度與資料覆蓋修正（2026-11-15）

## 1. 根因

| 問題 | 根因 | 位置 |
| --- | --- | --- |
| DELL 沒有收盤價 | 唯一的美股價格來源（Yahoo chart）條款無法確認，登錄檔維持 unknown；系統沒有備援與匯入機制。台股有價格，只因 TWSE OpenAPI 屬政府資料開放授權 | `sources.py`、`config/source-licenses.json` |
| 沒有歷史價格、共識、同業 | 沒有任何合法免費來源：TWSE 網站條款禁止自動下載（開放資料集只有最新交易日）；美股免費來源需 API Key 或條款不明 | `config/market-data-providers.json` 的 `rejected` |
| 情境 EPS 與倍數是手選 | `ScenarioProposal.valuation_metric_value` 由模型直接填寫，引擎只做乘法 | 舊 `scenarios.py` |
| 財務預測不連貫 | 沒有營收→利潤率→淨利→EPS、淨利→OCF→FCF 的推導鏈 | （新增 `forecast.py`） |
| Gate 只驗算術與揭露 | 沒有檢查假設是否落在歷史區間、是否外推極端成長、倍數是否有參照 | `quality_gates.py` |
| 產業驅動因素只寫在文字 | 設定檔只有說明文字，沒有計算 | `config/sector-profiles.json` |

## 2. 修正內容

- **市場資料層**（`market_data.py`）：依交易所設定的優先順序選擇來源；同一交易日的來源衝突會排除該日，不取平均；收盤價必須為正值且幣別正確；支援分割調整；過期判斷使用交易日曆；所有沒被採用的來源都附原因與補救方式，記錄在 `qa.json`。
- **交易日曆**（`calendars.py`）：NYSE 依交易所公布的固定規則計算；TWSE 使用官方開放資料的休市表（資料集 11761）。該資料集只發布最新年度，所以 2026 年標示為「未驗證」，產生 WARNING，不假設那一年沒有假日。
- **使用者匯入**（`cli import-market-data`、`bundle --operator-import`）：收盤價、分割、市場共識、同業倍數都可匯入，但必須附上使用者本人的權利聲明。AI 不能代為聲明。匯入的資料一律標為次級來源。
- **驅動因子預測**（`forecast.py`）：營收成長、營業利益率、業外占比、稅率、股數、折舊攤銷、資本支出、營運資金，每一項都標明出處。提案沒有填的項目會用歷史錨點補上，但極端的歷史成長率不能直接外推。現金流模型會附上與實際 TTM 營業現金流的校準差距。
- **倍數參照**（`multiples.py`）：歷史季末本益比至少 8 點、同業至少 3 家，才算「足夠」。只有倍數落在這個區間內，情境才算有證據；否則一律是假設性估值敏感度，以兩位有效數字呈現，不稱為目標價。
- **產業驅動因素**（`sector_drivers.py`）：
  - 半導體：資本支出強度、折舊攤銷占營收、新增營收的毛利率、先進製程占比
  - 硬體：存貨／應收／應付天數、現金轉換循環、積壓與訂單、融資應收，以及存貨增加是否有需求支撐的判斷
- **新增 6 項 Gate**，結果分四種狀態（PASS／WARNING／FAIL／NOT_APPLICABLE）：
  - `assumption_evidence_quality`
  - `scenario_internal_consistency`
  - `valuation_method_suitability`
  - `valuation_reference_quality`
  - `scenario_uncertainty_disclosure`
  - `forecast_period_consistency`

  標明了假設不等於假設被驗證：關鍵驅動因子是模型假設時給 WARNING；標為「歷史」卻落在歷史區間外、外推極端成長、缺少稅率時給 FAIL。

## 3. 新舊報告比較

| 項目 | TSMC 舊（v3） | TSMC 新（v4） | DELL 舊（v3） | DELL 新（v4） |
| --- | --- | --- | --- | --- |
| 情境 EPS | 100／125／145（直接指定） | 102.05／132.64／147.13（由驅動因子推導） | 14／20／25（直接指定） | 14.10／22.69／28.64（由驅動因子推導） |
| Base 營收成長依據 | 無 | +35%，引用第四季營收指引 | 無 | +30%，引用 Q3 營收指引 |
| 稅率、業外、股數 | 未建模 | 16.6%、3.8%、以最新股數為基準（歷史錨點） | 未建模 | 17.7%、−0.3%、以最新股數為基準 |
| FCF 預測 | 無 | 13,035／18,645／20,475 億元 | 無 | 63.2／143.4／182.5 億美元 |
| 倍數與價值 | 18／22／25 倍 → 1,800／2,750／3,625 元，未說明依據 | 18／22／24 倍，標為假設 → 1,800／2,900／3,500 元（假設性，兩位有效數字） | 10／13／15 倍 → 140／260／375 美元 | 10／13／15 倍，標為假設 → 140／290／430 美元（假設性，無參考價） |
| 產業驅動因素 | 只有文字 | 資本支出強度 33.3%、折舊攤銷 15.1%、新增營收毛利率 80.1%、先進製程 79% | 只有文字 | 存貨 64 天、應收 55 天、應付 150 天、現金轉換循環 −30 天；AI 積壓約為存貨 4.5 倍（但無前期可比）；融資應收 +43% |
| Gate | 13 項，二元結果 | 19 項，四種狀態；complete，2 項 WARNING | 13 項 | 19 項；limited，2 項 FAIL（價格）、1 項 WARNING |
| 頁數 | 6 | 6 | 6 | 7 |

## 4. 已解決與仍受外部資料限制的問題

**已解決（有測試，且報告中可見）**
- 情境由財務驅動因子推導，損益、EPS、現金流與每股價值互相勾稽（`scenario_internal_consistency`）
- 倍數沒有參照時，標為假設性敏感度，數值降低精度，不再呈現成目標價
- 假設的出處、與歷史區間的關係、極端成長外推、稅率缺漏，都由 Gate 檢查
- 價格缺失時，財務分析照常產出，但估值結論被擋下，並寫明原因與補救方式
- 交易日曆：NYSE 依規則計算，TWSE 依官方休市表；過期價格不會被當成最新
- 產業驅動因素有實際計算與數值

**仍受外部資料限制（已實作合法的匯入路徑，但沒有實際資料）**
- **DELL 收盤價與歷史價格**：沒有可自動取得且條款允許的免費來源。使用者以 `import-market-data` 匯入有權使用的資料後，估值與相對參考價會自動計算。
- **台股歷史價格**：TWSE 網站條款禁止自動下載。可改用使用者匯入，或之後逐日累積開放資料集的收盤價。
- **市場共識與同業倍數**：沒有合法的免費來源；匯入路徑會檢查會計口徑（GAAP 與 Non-GAAP 不互相比較）、期間與估計日期。
- **TWSE 2026 年休市表**：開放資料集只發布最新年度。

## 5. 使用者匯入

```sh
scripts/runtime.sh cli import-market-data --kind prices --csv prices.csv --ticker DELL --exchange NYSE \
  --attestation attestation.json --output reports/RUN/dell-prices.json
scripts/runtime.sh cli bundle ... --operator-import reports/RUN/dell-prices.json
```

- `prices.csv`：`date,close,currency[,adjustment]`
- `splits.csv`：`date,ratio`
- `consensus.csv`：`provider,metric,period_end,period_label,accounting_basis,value,unit,currency,estimate_date[,analyst_count]`
- `peers.csv`：`peer,metric,value,as_of`
- `attestation.json`：`attested_by`、`attested_at`、`rights_statement`、`source_name`、`source_url`。必須由使用者本人填寫。
