"""
calendar-sync：把專案的 ics 檔與 Google 日曆對帳。

用法：
  python sync_plan.py plan   <檔案.ics>                  列出差異（新增／修改／刪除），並輸出 plan JSON
  python sync_plan.py commit <檔案.ics> <results.json>   寫入成功後，把 event id 與指紋登記進對照表
  python sync_plan.py check  <檔案.ics> [--today YYYY-MM-DD] [--done <已完成.json>]
                                                        檢查日期：格式、文字週幾矛盾、同日到場、過期未確認、過期通知、沒寫來源

--done 指向另一份「已勾掉」清單（JSON，key 是 UID；例如桌面寵物的 已完成.json），裡面的事件視為已確認。

對照表放在 ics 旁邊：<檔名>.calendar_map.json
    { "<UID>": { "event_id": "...", "fingerprint": "...", "summary": "...", "when": "..." } }

results.json 格式（由 Claude 在呼叫日曆工具後產生）：
    { "created": { "<UID>": "<event_id>", ... }, "updated": ["<UID>", ...], "deleted": ["<UID>", ...] }
"""
import hashlib
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

TZ_OFFSET = "+08:00"  # Asia/Taipei


# ---------- ics 解析 ----------

def unfold(text: str) -> list[str]:
    """ics 規定超過 75 字元要折行，續行以空白或 tab 開頭；這裡先接回來。"""
    lines = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw.startswith((" ", "\t")) and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def unescape(value: str) -> str:
    return (value.replace("\\n", "\n").replace("\\N", "\n")
            .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\"))


def parse_duration_minutes(trigger: str) -> int | None:
    """把 -P1D、-PT30M、-PT2H 這類 TRIGGER 轉成「提前幾分鐘」。"""
    m = re.fullmatch(r"-?P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?", trigger.strip())
    if not m:
        return None
    d, h, mi = (int(x) if x else 0 for x in m.groups())
    return d * 1440 + h * 60 + mi


def parse_dt(params: str, value: str) -> tuple[str, bool]:
    """回傳 (ISO 字串, 是否全天)。全天回傳 YYYY-MM-DD，其餘回傳含 +08:00 的時間。"""
    if "VALUE=DATE" in params:
        return f"{value[0:4]}-{value[4:6]}-{value[6:8]}", True
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(Z?)", value)
    if not m:
        raise ValueError(f"看不懂的時間格式：{value}")
    y, mo, d, hh, mm, ss, z = m.groups()
    if z:  # UTC 的話直接標 Z，讓 Google 自己換算
        return f"{y}-{mo}-{d}T{hh}:{mm}:{ss}Z", False
    return f"{y}-{mo}-{d}T{hh}:{mm}:{ss}{TZ_OFFSET}", False


def parse_ics(path: Path) -> list[dict]:
    events, cur, in_alarm = [], None, False
    for line in unfold(path.read_text(encoding="utf-8")):
        if line == "BEGIN:VEVENT":
            cur, in_alarm = {"reminders": []}, False
            continue
        if line == "END:VEVENT":
            if cur is not None:
                events.append(cur)
            cur = None
            continue
        if cur is None:
            continue
        if line == "BEGIN:VALARM":
            in_alarm = True
            continue
        if line == "END:VALARM":
            in_alarm = False
            continue
        if ":" not in line:
            continue
        head, value = line.split(":", 1)
        name, _, params = head.partition(";")
        if in_alarm:
            if name == "TRIGGER":
                mins = parse_duration_minutes(value)
                if mins is not None:
                    cur["reminders"].append(mins)
            continue
        if name == "UID":
            cur["uid"] = value
        elif name == "SUMMARY":
            cur["summary"] = unescape(value)
        elif name == "DESCRIPTION":
            cur["description"] = unescape(value)
        elif name == "LOCATION":
            cur["location"] = unescape(value)
        elif name == "DTSTART":
            cur["start"], cur["all_day"] = parse_dt(params, value)
        elif name == "DTEND":
            cur["end"], _ = parse_dt(params, value)
    for e in events:
        for k in ("uid", "summary", "start", "end"):
            if k not in e:
                raise ValueError(f"事件缺 {k.upper()}（UID／SUMMARY／DTSTART／DTEND 都必填）：{e.get('summary', e)}")
    return events


def fingerprint(e: dict) -> str:
    key = json.dumps({
        "summary": e["summary"], "start": e["start"], "end": e["end"],
        "all_day": e["all_day"], "description": e.get("description", ""),
        "location": e.get("location", ""), "reminders": sorted(e["reminders"]),
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


# ---------- 對照表 ----------

def map_path(ics: Path) -> Path:
    return ics.with_name(ics.stem + ".calendar_map.json")


def load_map(ics: Path) -> dict:
    p = map_path(ics)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_map(ics: Path, data: dict) -> None:
    map_path(ics).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- plan ----------

def to_tool_args(e: dict) -> dict:
    """轉成 create_event／update_event 可以直接用的參數。"""
    args = {
        "summary": e["summary"],
        "startTime": e["start"],
        "endTime": e["end"],
        "timeZone": "Asia/Taipei",
    }
    if e["all_day"]:
        args["allDay"] = True
    if e.get("description"):
        args["description"] = e["description"]
    if e.get("location"):
        args["location"] = e["location"]
    if e["reminders"]:
        args["overrideReminders"] = [{"method": "popup", "minutes": m} for m in sorted(set(e["reminders"]))]
    return args


def when(e: dict) -> str:
    if e["all_day"]:
        return e["start"] + "（全天）"
    return e["start"][:16].replace("T", " ")


def cmd_plan(ics: Path) -> None:
    events = parse_ics(ics)
    mapping = load_map(ics)
    seen = set()
    create, update, unchanged = [], [], []
    for e in events:
        uid = e["uid"]
        if uid in seen:
            raise ValueError(f"UID 重複：{uid}")
        seen.add(uid)
        fp = fingerprint(e)
        entry = mapping.get(uid)
        item = {"uid": uid, "fingerprint": fp, "when": when(e), "args": to_tool_args(e)}
        if entry is None:
            create.append(item)
        elif entry["fingerprint"] != fp:
            item["event_id"] = entry["event_id"]
            item["old"] = f"{entry.get('when', '')} {entry.get('summary', '')}".strip()
            update.append(item)
        else:
            unchanged.append(item)
    delete = [{"uid": uid, "event_id": v["event_id"], "summary": v.get("summary", ""), "when": v.get("when", "")}
              for uid, v in mapping.items() if uid not in seen]

    print(f"來源：{ics}")
    print(f"對照表：{map_path(ics)}（{'已存在' if map_path(ics).exists() else '尚未建立'}）")
    print(f"事件 {len(events)} 筆：新增 {len(create)}、修改 {len(update)}、刪除 {len(delete)}、不變 {len(unchanged)}")
    print()
    if create:
        print("## 新增")
        for i in create:
            print(f"- {i['when']}  {i['args']['summary']}")
    if update:
        print("## 修改")
        for i in update:
            print(f"- {i['when']}  {i['args']['summary']}   （原：{i['old']}）")
    if delete:
        print("## 刪除（ics 裡已不存在）")
        for i in delete:
            print(f"- {i['when']}  {i['summary']}")
    plan_file = ics.with_name(ics.stem + ".calendar_plan.json")
    if not (create or update or delete):
        print("日曆已與 ics 一致，不需動作。")
        if plan_file.exists():
            plan_file.unlink()
        return
    plan_file.write_text(json.dumps(
        {"create": create, "update": update, "delete": delete}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"\nplan 已寫到：{plan_file}")


# ---------- commit ----------

def cmd_commit(ics: Path, results: Path) -> None:
    events = {e["uid"]: e for e in parse_ics(ics)}
    mapping = load_map(ics)
    res = json.loads(results.read_text(encoding="utf-8"))
    n_c = n_u = n_d = 0
    for uid, event_id in res.get("created", {}).items():
        e = events[uid]
        mapping[uid] = {"event_id": event_id, "fingerprint": fingerprint(e),
                        "summary": e["summary"], "when": when(e)}
        n_c += 1
    for uid in res.get("updated", []):
        e = events[uid]
        mapping[uid].update(fingerprint=fingerprint(e), summary=e["summary"], when=when(e))
        n_u += 1
    for uid in res.get("deleted", []):
        mapping.pop(uid, None)
        n_d += 1
    save_map(ics, mapping)
    print(f"對照表已更新：登記新增 {n_c}、修改 {n_u}、移除 {n_d}；目前共 {len(mapping)} 筆")
    plan_file = ics.with_name(ics.stem + ".calendar_plan.json")
    if plan_file.exists():
        plan_file.unlink()


# ---------- check ----------

WEEK = "一二三四五六日"
ACTION_PREFIX = re.compile(r"^【(?:截止|面試|提醒)】")
ACTION = re.compile(r"報名|上傳|繳費|郵寄|送出|面試|口試|報到")
INFO = re.compile(r"開放|開始|公告|放榜|查詢|列印")          # 只是通知、不用動手的事
PRESENCE = re.compile(r"面試|口試|報到|筆試|考試")              # 要人到場的事
SOURCE = re.compile(r"簡章|官網|公告|來源|https?://|系統|本人|使用者|助教|系辦|老師")
TEXT_DATE = re.compile(r"(?<![\d/\-])(\d{1,2})[/\-](\d{1,2})\s*[（(]\s*(?:週|星期)?([一二三四五六日天])\s*[）)]")


def event_date(e: dict) -> date:
    return date.fromisoformat(e["start"][:10])


def is_action(summary: str) -> bool:
    """要使用者動手的事：有【截止】【面試】【提醒】前綴，或含報名／上傳等字但不是「開放／公告」類通知。"""
    return bool(ACTION_PREFIX.match(summary)) or bool(ACTION.search(summary) and not INFO.search(summary))


def text_weekday_errors(e: dict) -> list[str]:
    """文字裡寫的「M/D（週X）」和那天真正的星期對不上就回報。"""
    out, base = [], event_date(e)
    for m in TEXT_DATE.finditer(e["summary"] + "\n" + e.get("description", "")):
        mo, d, wd = int(m.group(1)), int(m.group(2)), m.group(3).replace("天", "日")
        year = base.year + (1 if mo < base.month - 6 else 0)   # 12 月的事件提到 1 月，算隔年
        try:
            real = WEEK[date(year, mo, d).weekday()]
        except ValueError:
            continue
        if real != wd:
            out.append(f"寫「{m.group(0)}」，但 {year}/{mo}/{d} 其實是週{real}")
    return out


def cmd_check(ics: Path, today: date, done_file: Path | None) -> None:
    events = parse_ics(ics)
    done_uids = set(json.loads(done_file.read_text(encoding="utf-8"))) if done_file else set()
    fmt, wd_err, overdue, past_info, no_src = [], [], [], [], []
    by_day: dict[date, list[dict]] = {}
    for e in events:
        label = f"{when(e)}  {e['summary']}"
        d = event_date(e)
        if e["all_day"] and e["end"] <= e["start"]:
            fmt.append(f"{label}：全天事件的 DTEND 要寫結束日的隔天（單日就是 {d + timedelta(days=1)}），目前是 {e['end']}")
        elif e["end"] < e["start"]:
            fmt.append(f"{label}：DTEND 早於 DTSTART")
        for msg in text_weekday_errors(e):
            wd_err.append(f"{label}：{msg}")
        if ACTION_PREFIX.match(e["summary"]) and PRESENCE.search(e["summary"]) or \
                PRESENCE.search(e["summary"]) and not INFO.search(e["summary"]):
            by_day.setdefault(d, []).append(e)
        if d < today and "✅" not in e["summary"] and e["uid"] not in done_uids:
            (overdue if is_action(e["summary"]) else past_info).append(e)
        if d >= today and not SOURCE.search(e.get("description", "")):
            no_src.append(label)
    same_day = []
    for d, lst in sorted(by_day.items()):
        if len(lst) > 1:
            same_day.append(f"{d}（週{WEEK[d.weekday()]}）：" + "；".join(
                ("全天 " if x["all_day"] else x["start"][11:16] + " ") + x["summary"] for x in lst))

    print(f"檢查：{ics}（今天 {today}，事件 {len(events)} 筆）")
    if done_file:
        print(f"另採計已勾掉清單：{done_file}（{len(done_uids)} 筆）")
    sections = [
        ("格式問題", fmt, "改 ics"),
        ("文字與日期矛盾", wd_err, "回來源核對，改 ics"),
        ("同一天要到場的事", same_day, "請使用者確認只能去哪一個"),
        ("過期但沒確認做了沒", [f"{when(e)}  {e['summary']}   [UID {e['uid']}]" for e in overdue],
         "問使用者：做了就刪；沒做就連同同校系後續事件一起刪；要留紀錄才在 SUMMARY 前加 ✅"),
        ("已過去的通知類事件", [f"{when(e)}  {e['summary']}   [UID {e['uid']}]" for e in past_info],
         "直接從 ics 刪掉，不用問"),
        ("沒寫來源（只列未來的事）", no_src, "要「檢查日期正不正確」時回簡章／官網核對，並把來源補進 DESCRIPTION"),
    ]
    found = False
    for title, items, todo in sections:
        if not items:
            continue
        found = True
        print(f"\n## {title}（{len(items)}）  → {todo}")
        for i in items:
            print(f"- {i}")
    if not found:
        print("檢查通過：沒有格式問題、矛盾、同日到場、過期事件、缺來源。")


def main() -> None:
    if len(sys.argv) < 3 or sys.argv[1] not in ("plan", "commit", "check"):
        print(__doc__)
        sys.exit(1)
    ics = Path(sys.argv[2])
    if not ics.exists():
        sys.exit(f"找不到檔案：{ics}")
    if sys.argv[1] == "plan":
        cmd_plan(ics)
    elif sys.argv[1] == "check":
        opts = dict(zip(sys.argv[3::2], sys.argv[4::2]))
        today = date.fromisoformat(opts["--today"]) if "--today" in opts else date.today()
        done = Path(opts["--done"]) if "--done" in opts else None
        if done and not done.exists():
            sys.exit(f"找不到已完成清單：{done}")
        cmd_check(ics, today, done)
    else:
        if len(sys.argv) < 4:
            sys.exit("commit 需要 results.json")
        cmd_commit(ics, Path(sys.argv[3]))


if __name__ == "__main__":
    main()
