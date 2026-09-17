"""
calendar-sync：把專案的 ics 檔與 Google 日曆對帳。

用法：
  python sync_plan.py plan   <檔案.ics>                  列出差異（新增／修改／刪除），並輸出 plan JSON
  python sync_plan.py commit <檔案.ics> <results.json>   寫入成功後，把 event id 與指紋登記進對照表

對照表放在 ics 旁邊：<檔名>.calendar_map.json
    { "<UID>": { "event_id": "...", "fingerprint": "...", "summary": "...", "when": "..." } }

results.json 格式（由 Claude 在呼叫日曆工具後產生）：
    { "created": { "<UID>": "<event_id>", ... }, "updated": ["<UID>", ...], "deleted": ["<UID>", ...] }
"""
import hashlib
import json
import re
import sys
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


def main() -> None:
    if len(sys.argv) < 3 or sys.argv[1] not in ("plan", "commit"):
        print(__doc__)
        sys.exit(1)
    ics = Path(sys.argv[2])
    if not ics.exists():
        sys.exit(f"找不到檔案：{ics}")
    if sys.argv[1] == "plan":
        cmd_plan(ics)
    else:
        if len(sys.argv) < 4:
            sys.exit("commit 需要 results.json")
        cmd_commit(ics, Path(sys.argv[3]))


if __name__ == "__main__":
    main()
