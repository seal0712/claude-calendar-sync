---
name: calendar-sync
description: 把專案的重要日期同步到 Google 日曆（新增、修改、刪除都會對帳，不會重複建立）。只要使用者說「加進日曆」「同步日曆」「日期定案了」「幫我記在行事曆」，或對話中剛剛確定／更新了一個有明確日期的截止、面試、放榜、活動、繳費期限，就用這個 skill，即使他沒明講「日曆」。適用所有專案（推甄、競賽、交易、論文等），每個專案各有自己的 ics 檔。
---

# calendar-sync：專案重要日期同步到 Google 日曆

核心是「對帳」不是「新增」。每個專案有一個 ics 檔當作日期的唯一來源，
旁邊的 `<檔名>.calendar_map.json` 記住每筆事件對應的 Google 事件 ID。
重跑時只動有差異的事件，日曆不會越加越亂。

## 流程

### 第一步：找到或建立該專案的 ics

- 在該專案資料夾找 `*.ics`；沒有就建一個，命名 `<專案名>重要日期.ics`，放在專案根目錄。
- 記憶裡若記有各專案 ics 的位置，優先用記憶裡的。
- 使用者說的專案不確定是哪個資料夾時，先問，不要猜。

### 第二步：把新日期寫進 ics（若日期來自對話或 md，而不是 ics 本身）

- 修改前先備份：複製一份為 `<檔名>.bak_YYYYMMDD.ics`。
- 每筆 VEVENT 四個欄位必填：`UID`、`SUMMARY`、`DTSTART`、`DTEND`。
- UID 建好就不要再改，它是對帳的鍵；改了會被當成「刪一筆、新增一筆」。
  格式：`<uuid4>@<專案代號>`。
- 有明確時間：`DTSTART;TZID=Asia/Taipei:20261030T083000`，DTEND 同格式。
  截止類事件用截止時刻當 DTSTART、往後 30 分鐘當 DTEND；面試用實際時段。
- 全天：`DTSTART;VALUE=DATE:20261028`、`DTEND;VALUE=DATE:20261029`（結束日是隔天，這是 ics 的規定）。
- 提醒：`BEGIN:VALARM` / `TRIGGER:-P1D` / `ACTION:DISPLAY` / `END:VALARM`。截止類一律 `-P1D`。
- 有地點就寫 `LOCATION:`，日曆上可以直接導航。
- 標題慣例：`【截止】`、`【面試】`、`【提醒】`、`【待定】` 開頭，後接校名或專案名，讓月曆一眼看得出類型。
- 日期一律要有來源（簡章、官網、主辦方公告）。使用者口頭說的日期照寫，但若與檔案裡既有記錄衝突，先指出再改。

### 第三步：跑 plan，把差異表給使用者看

```
python ~/.claude/skills/calendar-sync/scripts/sync_plan.py plan <檔案.ics>
```

輸出「新增／修改／刪除／不變」四類清單，並寫出 `<檔名>.calendar_plan.json`。
用 AskUserQuestion 工具讓使用者打勾確認，不要用文字清單問：
- `multiSelect: true`，每個事件一個選項，label 寫「M/D（週幾）標題」（截止類加時間），description 一句話說明或留空。
- 一題最多 4 個選項、一次最多 4 題：超過 4 筆就拆成多題，header 用「新增 1/2」「修改」「刪除」這類分組；超過 16 筆才退回文字清單。
- 使用者要求以外多列的日期，label 尾加「（多列）」。
- 修改類 label 寫「舊日期 → 新日期 標題」；刪除類 label 寫「刪除：標題」。
- 使用者沒勾的：不寫入、不登記，並且從 ics 移除（不然每次對帳都會再問一次）。他之後要加再寫回去。
這一步一定要停，日期寫錯等於錯過截止。若顯示「日曆已與 ics 一致」就直接回報，不用問。

### 第四步：依 plan 呼叫日曆工具

先載入工具（一次載齊）：
```
ToolSearch select:mcp__claude_ai_Google_Calendar__create_event,mcp__claude_ai_Google_Calendar__update_event,mcp__claude_ai_Google_Calendar__delete_event
```

讀 `<檔名>.calendar_plan.json`：
- `create[]`：每筆的 `args` 直接展開當 `create_event` 參數。記下回傳的 `id`。
- `update[]`：`update_event`，`eventId` 用該筆的 `event_id`，其餘欄位從 `args` 帶入
  （`summary`、`startTime`、`endTime`、`allDay`、`description`、`location`、`overrideReminders`、`timeZone`）。
  沒改到的欄位不傳也沒關係，工具只更新有給的欄位。
- `delete[]`：`delete_event`，`eventId` 用該筆的 `event_id`。

預設寫進使用者的主日曆，不用傳 `calendarId`；使用者指定別的日曆時，先用 `list_calendars` 查 ID。
獨立的呼叫可以同一批平行送出。
任何一筆失敗就停下來回報，不要繼續，也不要跑第五步；失敗的那筆不會被登記，下次重跑會再出現。

### 第五步：commit，把結果登記回對照表

把結果整理成 results.json（放暫存區）：
```json
{ "created": { "<UID>": "<回傳的 event id>" }, "updated": ["<UID>"], "deleted": ["<UID>"] }
```
```
python ~/.claude/skills/calendar-sync/scripts/sync_plan.py commit <檔案.ics> <results.json>
```
commit 會更新對照表並刪掉 plan 檔。**這步不能漏**，漏了下次會重複建立。

### 第六步：回報

- 列出實際新增／修改／刪除的筆數與標題。
- 告訴使用者怎麼確認：打開 Google 日曆切到那個月份，應該看到哪幾天有什麼。
- 最近的一筆是哪天、提醒會在哪天跳出來。

## 注意

- 對照表 `*.calendar_map.json` 是狀態檔，不要手動編輯、不要刪。刪了就會全部重複建立。
- ics 只能用 `python sync_plan.py plan` 對帳，不要用 Google 日曆的「匯入」功能，那會重複建立且對照表不會知道。
- 使用者在 Google 日曆上手動改了事件（例如拖到別天），對照表不會知道；下次 ics 有變動時會以 ics 為準蓋回去。要改就改 ics。
- 對照表裡已登記但使用者在日曆上手動刪掉的事件，`update_event` 會失敗；這時把該 UID 從對照表移除後重跑 plan，會變成新增。
