"""Одноразовый запуск: проверить график, ответить на команды, написать об изменениях."""
import os, json, time, hashlib, datetime as dt
from zoneinfo import ZoneInfo
import httpx

TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = int(os.environ["CHAT_ID"])
THREAD_ID = int(os.getenv("THREAD_ID") or 0) or None   # ID темы в группе (необязательно)
GROUP = os.getenv("GROUP", "GPV3.2")
URL = os.getenv(
    "DATA_URL",
    "https://raw.githubusercontent.com/Baskerville42/outage-data-ua/main/data/kyiv-region.json",
)
TZ = ZoneInfo("Europe/Kyiv")
API = f"https://api.telegram.org/bot{TOKEN}"
STATE_FILE = "state.json"

HOUR = {
    "yes": ("on", "on"), "no": ("off", "off"),
    "first": ("off", "on"), "second": ("on", "off"),
    "maybe": ("maybe", "maybe"),
    "mfirst": ("maybe", "on"), "msecond": ("on", "maybe"),
}
ICON = {"off": "🔴", "on": "🟢", "maybe": "🟡"}
WEEKDAYS = ["понеділок", "вівторок", "середа", "четвер", "п’ятниця", "субота", "неділя"]


FLOOD_UNTIL = 0.0   # до какого момента Telegram просит бота помолчать (unix-время)


class FloodWait(Exception):
    """Telegram ответил 429 с большой паузой — сейчас отправлять нельзя."""


def tg(method, **params):
    global FLOOD_UNTIL
    for attempt in range(2):
        r = httpx.post(f"{API}/{method}", json=params, timeout=30)
        if r.status_code == 429:
            try:
                wait = int(r.json().get("parameters", {}).get("retry_after", 5))
            except Exception:
                wait = 5
            if wait <= 10 and attempt == 0:   # короткую паузу просто пережидаем
                time.sleep(wait + 1)
                continue
            FLOOD_UNTIL = max(FLOOD_UNTIL, time.time() + wait + 5)
            raise FloodWait(wait)
        r.raise_for_status()
        return r.json()


def send(text, thread=None):
    params = {"chat_id": CHAT_ID, "text": text}
    if thread:
        params["message_thread_id"] = thread
    return tg("sendMessage", **params)["result"]["message_id"]


def delete(message_id):
    """Удалить своё сообщение; любая ошибка (старше 48 ч, уже удалено) игнорируется."""
    if not message_id:
        return
    try:
        tg("deleteMessage", chat_id=CHAT_ID, message_id=message_id)
    except Exception as e:
        print("delete failed:", e)


def load_state():
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {"hash": "", "status": None, "offset": 0}


def save_state(st):
    json.dump(st, open(STATE_FILE, "w"), indent=1)


def fetch():
    r = httpx.get(URL, timeout=30)
    r.raise_for_status()
    return r.json()


def day_slots(data, day):
    """48 получасовых слотов суток: on / off / maybe. None, если данных нет."""
    hours = None
    for ts, groups in data.get("fact", {}).get("data", {}).items():
        if dt.datetime.fromtimestamp(int(ts), TZ).date() == day and GROUP in groups:
            hours = groups[GROUP]
            break
    if hours is None:
        hours = (data.get("preset", {}).get("data", {}).get(GROUP, {})
                 .get(str(day.isoweekday())))
    if not hours:
        return None
    slots = []
    for h in range(1, 25):
        slots += HOUR.get(hours.get(str(h), "yes"), ("on", "on"))
    return slots


def fmt(i):
    return f"{i // 2:02d}:{(i % 2) * 30:02d}"


def dur(n):
    h = n / 2
    s = str(int(h)) if h == int(h) else str(h).replace(".", ",")
    return f"{s} год."


def segments(slots):
    segs, start = [], 0
    for i in range(1, 49):
        if i == 48 or slots[i] != slots[start]:
            segs.append((start, i, slots[start]))
            start = i
    return segs


def day_text(data, day, title):
    title = f"{title} ({day:%d.%m} {WEEKDAYS[day.weekday()]})"
    slots = day_slots(data, day)
    if not slots:
        return f"{title}: даних немає"
    lines = []
    for a, b, k in segments(slots):
        end = fmt(b) if b < 48 else "24:00"
        tail = " можливо" if k == "maybe" else ""
        lines.append(f"{ICON[k]} {fmt(a)}–{end} ({dur(b - a)}){tail}")
    off = sum(1 for s in slots if s == "off")
    if off:
        lines.append(f"Без світла: {dur(off)}")
    return f"{title}:\n" + "\n".join(lines)


def full_text(data):
    today = dt.datetime.now(TZ).date()
    return (f"⚡ Група {GROUP.replace('GPV', '')}, Київська обл.\n\n"
            + day_text(data, today, "Сьогодні") + "\n\n"
            + day_text(data, today + dt.timedelta(days=1), "Завтра"))


def status_now(data, now):
    """Описание текущего статуса: состояние, длительность, когда сменится, что было до этого."""
    today = now.date()
    y = day_slots(data, today - dt.timedelta(days=1))
    t = day_slots(data, today)
    if not t:
        return None
    n = day_slots(data, today + dt.timedelta(days=1))
    slots = (y or []) + t + (n or [])
    base = len(y or [])
    i = base + now.hour * 2 + (now.minute >= 30)
    cur = slots[i]

    a = i
    while a > 0 and slots[a - 1] == cur:
        a -= 1
    b = i + 1
    while b < len(slots) and slots[b] == cur:
        b += 1

    until = None
    if b < len(slots):
        k = b - base
        until = fmt(k % 48) + (" (завтра)" if k >= 48 else "")
    # длительность показываем, только если известны оба края интервала
    dur_n = (b - a) if (a > 0 and b < len(slots)) else None

    prev_kind = prev_dur = None
    if a > 0:
        prev_kind = slots[a - 1]
        c = a - 1
        while c > 0 and slots[c - 1] == prev_kind:
            c -= 1
        if c > 0:
            prev_dur = a - c

    return {"cur": cur, "until": until, "dur": dur_n,
            "prev_kind": prev_kind, "prev_dur": prev_dur}


def status_text(info):
    cur, until, dur_n = info["cur"], info["until"], info["dur"]
    tail = f" (тривалість {dur(dur_n)})" if dur_n else ""
    if cur == "off":
        s = "🔴 Світла немає"
        if until:
            s += f", увімкнення о {until}"
        return s + tail
    if cur == "on":
        s = "🟢 Світло є"
        if until:
            s += f", відключення о {until}"
        s += tail
        if info["prev_kind"] == "off" and info["prev_dur"]:
            s += f"\nБез світла було {dur(info['prev_dur'])}"
        return s
    return "🟡 Можливе відключення"


def handle_commands(data, st, now):
    ups = tg("getUpdates", offset=st.get("offset", 0), timeout=0)["result"]
    todo = {}
    for u in ups:
        st["offset"] = u["update_id"] + 1
        m = u.get("message") or {}
        if m.get("chat", {}).get("id") != CHAT_ID:
            continue
        words = (m.get("text") or "").split()
        cmd = words[0].split("@")[0].lower() if words else ""
        if cmd in ("/today", "/grafik", "/now"):
            kind = "now" if cmd == "/now" else "today"
            todo[(kind, m.get("message_thread_id"))] = None  # одинаковые команды = один ответ

    save_state(st)  # offset сохраняем ДО отправки, чтобы сбой не вызвал повторных ответов

    new_ids = []
    for kind, thread in todo:
        try:
            if kind == "today":
                mid = send(full_text(data), thread)
            else:
                info = status_now(data, now)
                mid = send(status_text(info) if info else "Даних немає", thread)
            new_ids.append(mid)
        except FloodWait:
            print("flood wait — остальные ответы пропущены")
            break
        except Exception as e:
            print("send failed:", e)
        time.sleep(1.5)

    if new_ids:  # прошлые ответы бота на команды удаляем
        for old_id in st.get("reply_msgs", []):
            delete(old_id)
        st["reply_msgs"] = new_ids


def main():
    st = load_state()
    if st.get("blocked_until", 0) > time.time():
        print("Telegram просит подождать (flood wait) — запуск пропущен")
        return
    data = fetch()
    now = dt.datetime.now(TZ)

    try:
        handle_commands(data, st, now)
    except Exception as e:
        print("commands failed:", e)

    # 1) график изменился
    text = full_text(data)
    h = hashlib.md5(text.encode()).hexdigest()
    if h != st.get("hash"):
        prev_hash = st.get("hash")
        st["hash"] = h
        save_state(st)  # «график отправлен» фиксируем ДО отправки: лучше пропустить, чем засыпать чат
        new_id = None
        try:
            new_id = send("🔔 Графік оновлено\n\n" + text, THREAD_ID)
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            print("schedule send failed:", code)
            if code == 429 or code >= 500:  # точно не доставлено — повторим на следующем запуске
                st["hash"] = prev_hash
                save_state(st)
        except FloodWait:  # Telegram просит подождать: не доставлено, повторим после паузы
            print("schedule send: flood wait")
            st["hash"] = prev_hash
            save_state(st)
        except Exception as e:  # таймаут и т.п.: неясно, дошло ли — повтор не делаем
            print("schedule send failed:", e)
        if new_id:
            delete(st.get("schedule_msg"))  # предыдущее такое же сообщение — убираем
            st["schedule_msg"] = new_id
            save_state(st)

    # 2) свет по графику включился / выключился
    try:
        info = status_now(data, now)
        if info and info["cur"] in ("on", "off") and info["cur"] != st.get("status"):
            if st.get("status") is not None:
                new_id = send(status_text(info), THREAD_ID)
                delete(st.get("status_msg"))
                st["status_msg"] = new_id
            st["status"] = info["cur"]
    except Exception as e:
        print("status failed:", e)

    if FLOOD_UNTIL:
        st["blocked_until"] = FLOOD_UNTIL
    save_state(st)


if __name__ == "__main__":
    main()
