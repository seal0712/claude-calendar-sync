# calendar-sync：讓 Claude Code 把專案重要日期同步到 Google 日曆

把截止日、面試、活動日期交給 Claude，它會先列出要加的事件讓你打勾確認，再寫進 Google 日曆。
日期改了重跑一次，只會修改有變動的事件，不會越加越多重複。

## 事前準備（三樣）

1. **Claude Code**（終端機版或桌面版都可以）。
2. **Google 日曆連接器**：到 claude.ai → 設定 → 連接器（Connectors），連上 Google Calendar。
   Claude Code 會自動看到這些工具，不用另外設定。
3. **Python 3.10 以上**：終端機打 `python --version` 有出現版本號就可以。

## 安裝

**方法一（最簡單）**：點本頁右上角綠色「Code」→「Download ZIP」，解壓後把裡面的資料夾改名為 `calendar-sync`，放到：

- Windows：`C:\Users\<你的帳號>\.claude\skills\calendar-sync\`
- Mac／Linux：`~/.claude/skills/calendar-sync/`

**方法二**：終端機執行
```
git clone https://github.com/seal0712/claude-calendar-sync ~/.claude/skills/calendar-sync
```

放好後重開 Claude Code，在對話裡打 `/calendar-sync` 有反應就代表裝好了。

## 怎麼用

在 Claude Code 裡直接講，例如：

- 「把期末報告截止 12/20 加進日曆」
- 「這份簡章的日期幫我同步到日曆」（把簡章 PDF 或 md 給它）
- 「決賽改到 11/8 了，更新日曆」

Claude 會：
1. 在你的專案資料夾建（或更新）一個 `<專案名>重要日期.ics`
2. 列出「新增／修改／刪除」清單讓你打勾
3. 你勾完才寫進日曆
4. 把結果記在旁邊的 `<專案名>重要日期.calendar_map.json`

## 兩個檔案的意思

- `*.ics`：日期的唯一來源，可以自己開來看，也可以匯入其他行事曆軟體。
- `*.calendar_map.json`：記住每筆事件在 Google 日曆上的 ID。**不要刪、不要手動改**，刪了下次會全部重複建立。

## 注意

- 要改日期就跟 Claude 說，讓它改 ics；不要直接在 Google 日曆上拖動事件，下次同步會被 ics 蓋回去。
- 不要用 Google 日曆的「匯入 ics」功能匯同一個檔，會重複。
- 時區寫死在台灣（`Asia/Taipei`）。其他時區的話，改 `scripts/sync_plan.py` 開頭的 `TZ_OFFSET`，以及 `to_tool_args()` 裡的 `timeZone`。
- 預設寫進主日曆。想寫進別的日曆，跟 Claude 說日曆名稱就好。
