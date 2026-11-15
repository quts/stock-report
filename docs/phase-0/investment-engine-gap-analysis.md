# 投資研究引擎：差距分析與架構決策（2026-11-15）

> 範圍：把現有個股管線由「財報資訊摘要」升級為「以證據為基礎的投資研究引擎」，台股與美股共用同一框架。
> 依據：使用者 2026-11-15 任務說明；仍為 Phase 0，不改變權限、時點與私人使用的既有契約。

## 1. 現況盤點

| 面向 | 現有實作 | 主要限制 |
| --- | --- | --- |
| 採集與來源選擇 | `collect.py`、`sources.py`（TWSE/TPEx OpenAPI、SEC XBRL 年度、Yahoo 價格〔封閉〕）、`filing_collect.py`（審閱過的申報逐字摘錄）、`reviewed_collect.py` | XBRL 只取最近兩個**年度**；TW 只取最新一期年初至今累計；沒有欄位層級的備援順序 |
| 財報正規化 | `Evidence` 有期間起訖、`period_basis`、單位、幣別、`statement_kind` | 沒有會計基礎（GAAP／IFRS／Non-GAAP）欄位；沒有會計年度標籤；YTD 不能換算單季 |
| 市價 | TWSE 最新收盤（開放）；美股價格來源未通過條款查核 | 美股沒有合法免費價格；台股沒有歷史價格；沒有交易日曆檢查 |
| 共識與分析師預測 | `statement_kind` 可標記 `analyst_estimate`／`consensus_estimate`，但沒有已查核來源 | 沒有比較邏輯（指引 vs 共識 vs 實績） |
| 推理 Prompt | `analysis.py` PROMPT v4：六段 `company_research` | 沒有投資論點、情境或假設欄位；LLM 無確定性估值可引用 |
| 估值 | `quant.py`：P/E（需 TTM EPS）、價格變動 | 沒有市值、EV、FCF、殖利率、情境估值；P/E 實務上幾乎都是 Not Applicable |
| 報告 | `research-four.html`（四頁）、`report.html`（兩頁） | 四頁上限擠掉估值與情境；沒有圖表 |
| 缺值處理 | `Metric.missing_reason` 文字；Gate 降級 insufficient | 不區分不適用／未揭露／找不到／來源不可用／衝突／過期；缺值靜默消失 |
| 測試與品質檢查 | 授權、時點、身份、引用 ID 的 Gate | 沒有檢查正文數字是否有證據；沒有估值假設揭露檢查；沒有報告分級 |

## 2. 共同根因（依優先順序）

1. **沒有時間序列模型**：財報被存成零散的單筆證據，無法組出 8 季序列、TTM 或 YTD 推導單季，所以成長加速度、現金轉換、FCF 與估值都無從計算。（DELL 缺估值、TSMC 缺 FCF 的共同原因）
2. **缺確定性的估值層**：只有一個 P/E 函式，沒有「方法選擇＋輸入可得性＋限制揭露」的引擎，LLM 只能寫描述。
3. **缺完整性模型**：沒有「研究需要哪些欄位」的宣告，缺值只會讓分析變淺，不會出現在報告的品質警告中。
4. **提案結構沒有投資問題**：沒有論點、情境、假設的型別，也就無法驗證假設是否揭露、情境是否自洽。
5. **品質 Gate 只檢查權限與時點，不檢查研究品質**：正文數字、估值假設、章節矛盾都沒有檢查。

## 3. 最小架構變更（不重寫既有管線）

新增五個純函式模組，由 `finalize` 在渲染前呼叫；既有 Gate、授權與時點規則不變：

| 模組 | 責任 |
| --- | --- |
| `research_contract.py` | 六類資料（事實／指引／共識／假設／衍生／解讀）、資料狀態（7 種）、會計基礎、會計年度與正規期間標籤、相容性檢查 |
| `fin_series.py` | 由證據組出季度序列（單季、YTD 推導單季、TTM），偵測同期衝突 |
| `completeness.py` | 依產業設定宣告必要欄位，判定每個欄位的狀態並產生品質警告 |
| `fin_analysis.py` | 成長與加速度、三率、EPS 與盈餘品質、OCF/NI、FCF 與轉換率、CapEx 強度、營運資金、流動性與槓桿、ROIC（可算時） |
| `valuation.py` | 依產業與資料品質選方法（P/E、Forward P/E、EV/EBITDA、P/FCF、FCF Yield、P/B、歷史區間、同業、情境），不可算時寫明原因 |
| `scenarios.py` | 驗證並計算 Bull／Base／Bear 的隱含股價、相對參考價、敏感度；不接受無來源的機率 |
| `expectations.py` | 實績 vs 先前指引、指引 vs 共識、共識修正、實績 vs 共識（同指標、同期間、同會計基礎才比較） |
| `quality_gates.py` | 發布前檢查；任一關鍵檢查失敗即改為「受限報告」，不呈現為完整投資報告 |

產業差異寫在 `config/sector-profiles.json`（要看的指標、適用估值方法、解讀重點），不寫在程式碼的公司判斷式裡。

## 4. 取捨

- **確定性優先**：所有數字由 Python 計算；LLM 只能提出假設與解讀，正文的數字必須能回溯到證據或計算結果。
- **不付費、不加金鑰**：美股價格與市場共識目前沒有通過條款查核的免費來源，因此估值會標示不可用，而不是改用未查核的網站。
- **報告長度**：投資版型（`report_profile=investment`）允許最多 8 頁；四頁版型保留，既有行為不變。

## 5. 已實作（2026-11-15）

| 需求 | 實作 | 檔案 |
| --- | --- | --- |
| 投資導向資料契約 | 六類資料、7 種資料狀態、`accounting_basis`、`target_period_end`、會計年度正規標籤、`DataGap` | `research_contract.py`、`models.py` |
| 時間序列 | 單季／YTD／年度／時點分開；YTD 推導單季；TTM（四季或「全年＋YTD−去年 YTD」）；衝突偵測；折舊＋攤銷合併 | `fin_series.py` |
| 美股季度資料 | SEC XBRL `series=quarterly`（同一已開放端點，不新增來源） | `sources.py` |
| 完整性與復原 | 依產業宣告必要欄位；已記錄的取得失敗（`source_unavailable`）不當作「不存在」 | `completeness.py`、`bundle --data-gaps` |
| 財務分析 | 成長與加速度、三率與四季變化、EPS 成長、OCF/NI、FCF 與轉換率、CapEx 強度、營運資金、淨負債與槓桿；ROIC 不以假設稅率估算 | `fin_analysis.py`、`config/sector-profiles.json` |
| 估值 | 依產業選方法；價格過期或缺失時全部標示；YTD 不年化；金融業改用 P/B | `valuation.py` |
| 情境 | Bull／Base／Bear；假設逐項標示基礎；隱含股價、相對參考價、3×3 敏感度；不接受機率 | `scenarios.py`、`Proposal.scenarios` |
| 指引與共識 | 同指標、同期間、同單位、同會計基礎才比較；沒有共識時明說 | `expectations.py` |
| 事件重要性 | 依六個投資影響面向計分；低影響事件移到附錄 | `materiality.py`、`StockEvent.impact_areas` |
| 品質 Gate | 13 項檢查；關鍵失敗即 `limited`，首頁標示 | `quality_gates.py` |
| 報告 | `report_profile=investment`，4–8 頁，含營收／EPS、利潤率與情境圖（內嵌 SVG，無新依賴） | `templates/research-investment.html`、`charts.py` |

## 6. 仍未解決的資料依賴

- **美股價格**：沒有通過條款查核的免費來源（Yahoo chart 條款未確認、Nasdaq／NYSE 條款限制程式擷取），美股估值與情境的相對漲跌目前都無法計算。
- **歷史股價與歷史估值區間**：台股 OpenAPI 只有最新交易日；兩個市場都沒有已查核的歷史序列。
- **市場共識與同業估值**：沒有合法免費、條款清楚的來源；引擎與報告會照實標示「未找到」。
- **ROIC**：需要有效稅率與投入資本的完整拆分，本版不以假設稅率估算。
- **股數**：以最新一季加權平均稀釋股數代替期末流通股數，報告已逐處註明。
- **交易日曆**：以週一至週五估算，未納入交易所假日。
