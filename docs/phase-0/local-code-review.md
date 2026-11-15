# 開 PR 前的本機 Code Review

日期：2026-11-15（Asia/Taipei）。Reviewer：Codex。比較範圍：`main...codex/phase-0`；以 `a0027f2` 為 review 起點，以下修正納入本次 PR。這是風險導向的本機審查，不是投資效能、法律授權或完整 MVP 的簽認。

## 審查結果與修正

| 嚴重度 | 發現 | 修正及回歸證據 |
| --- | --- | --- |
| P1 | 舊 `probe` 指令在所有來源 unknown 時，仍落盤交易所樣本值、SEC 身份及 transport 回應雜湊；例外文字也可能包含來源內容 | `providers.probes` 改成只記錄數量、身份存在布林、固定傳輸欄位及例外類型；測試注入私有內容與雜湊，確認檔案不留存 |
| P1 | `quant.usable` 僅檢查 internal-use 布林，未要求 confirmed 或相同 research_as_of；受限數值可能先進入內部指標與 Gate 分歧判定，僅靠 PDF 過濾不足 | 量化入口直接排除 unknown、restricted 與不同研究截止時點；三項回歸測試確認指標及引用均排除。行情公式測資明示虛構 confirmed，沒有修改真實登錄檔 |

另外檢查了 prepare/finalize/replay 的快照與版本驗證、個人 PDF 的證據投影與資源抓取禁用、採集與 bundle 的來源範圍和權限交集、事件卷宗的人工覆核與已關閉來源阻擋、RSS 有界解析、機構評等去重，以及 activation-check 的唯讀與限定權限行為。以上檢查未再發現本輪必須修復的阻斷問題；不代表自由文字推論或所有上游資料均可自動驗證。

## 驗收與發布範圍

最終離線回歸為 441 passed、10 deselected；執行證據在本機 `reports/phase-0/acceptance.json` 與 `provider-increment-junit.xml`。第一輪回歸揭露行情公式測資只有 internal-use=true、授權狀態卻仍 unknown；已補成明示已確認的虛構測資後重跑完整驗收。沒有放寬產品授權條件。

既有十例 Codex 互動紀錄仍是歷史批次；本輪修正以確定性回歸及完整離線管線驗收，不宣稱重新執行模型評估。舊成品的程式雜湊保留不動，不以新程式冒充舊版本 Replay。

PR 只包含程式、合成測資及開發驗證摘要。本機 `reports/`、PDF、研究 JSON 與 SQLite 不在 Git 追蹤範圍；不推送這些私人產物。未啟用來源、未呼叫付費 API、未安裝主機依賴、未合併 PR。

## 人工核對順序

1. `docs/phase-0/pre-human-review.md`：先看可核對範圍與不可由人猜測的上游缺口。
2. `docs/phase-0/2330-source-activation.json`：只核對 `twse-openapi-stock-day-all` 的身份、欄位、時間限制、proposed_scope、顯名稿及 human_checklist。財報項目標為不建議啟用。
3. `docs/phase-0/taiwan-license-review-evidence.md` 與 `taiwan-source-binding.json`：親自確認所列官方資料集／條款頁、CSV 與 JSON 的有限欄位對應。記錄實際查核者、時間及條文，不把助理查核當成人工簽認。
4. `config/tw-instruments.json`：核對 2330 的交易市場、代號、名稱／股類設定。此輪有限啟用包不授權 6488 或其他來源。
5. `config/source-licenses.json`：確認六個來源目前仍 unknown、所有 scope 關閉、沒有人工 attestation。無須自行修改；完成核對且另行明示啟用後，才由 Codex 準備候選登錄檔並執行 activation-check。
6. 本機 `reports/phase-0/sample-stock.pdf` 與 `sample-event.pdf`：檢查繁體中文字體、兩頁閱讀性、兩個期限及個人使用標示。兩份都是合成示範，不能作為真實研究或來源授權證明。

目前可交付人工核對的是有限 2330 身份與最新收盤來源。即使通過，也只能支持資料不足摘要；尚不能保證方向性研究完成。真實資料 E2E、SEC 聯絡設定後的網路驗收、未有官方定義的財報／調整價格欄位，以及真實事件研究，仍各有獨立條件；不能說整個產品只差一次人工簽名。
