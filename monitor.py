"""銀山温泉 空室監視 → Discord通知

GitHub Actions で定期実行し、指定日に「夕食付き」の空室が出たら Discord に通知する。
"""
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

# ===== 設定 =====
CHECK_IN = os.getenv("CHECK_IN", "2026-10-23")     # 宿泊日
ADULTS = int(os.getenv("ADULTS", "2"))
DINNER_ONLY = os.getenv("DINNER_ONLY", "1") == "1"  # 夕食付きのみ通知
LOOP_MINUTES = float(os.getenv("LOOP_MINUTES", "0"))  # 0なら1回だけ
INTERVAL_SEC = int(os.getenv("INTERVAL_SEC", "60"))
WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "")
STATE_FILE = Path("state.json")

# 489ban（各宿の公式予約システム）
BAN489 = {
    "takimikan": "瀧見舘",
    "notoyaryokan": "能登屋旅館",
    "ginzanso": "銀山荘",
    "kosekiya": "古勢起屋別館",
}
# 楽天トラベル（施設番号）
RAKUTEN = {
    "184539": "本館古勢起屋",
    "137447": "古山閣",
    "68602": "旅館藤屋",
    "176672": "旅籠いとうや",
    "52933": "旅館松本",
    "111234": "銀山荘",
    "111235": "古勢起屋別館",
    "183204": "能登屋旅館",
}
# じゃらん（宿番号）
JALAN = {
    "356689": "古山閣 新館 クラノバ",
}

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36")
JST = timezone(timedelta(hours=9))

d_in = date.fromisoformat(CHECK_IN)
d_out = d_in + timedelta(days=1)


def text_of(html: str) -> str:
    html = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", html)
    t = t.replace("&nbsp;", " ").replace("&times;", " ")
    return re.sub(r"\s+", " ", t)


def session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = UA
    return s


# ---------- 489ban ----------
def check_489ban(slug: str, name: str) -> list[dict]:
    s = session()
    ds = d_in.strftime("%Y/%m/%d")
    base = f"https://reserve.489ban.net/client/{slug}/0"
    page_url = f"{base}/plan/search?date={requests.utils.quote(ds, safe='')}&roomCount=1"
    r = s.get(page_url, timeout=30)
    r.raise_for_status()
    m = re.search(r'name="csrf-token" content="([^"]+)"', r.text)
    if not m:
        raise RuntimeError("csrf-token not found")
    r = s.post(
        f"{base}/planlist/search?date={requests.utils.quote(ds, safe='')}&roomCount=1",
        headers={"X-CSRF-TOKEN": m.group(1), "X-Requested-With": "XMLHttpRequest",
                 "Referer": page_url},
        timeout=30,
    )
    r.raise_for_status()
    html = r.json().get("planList", "")
    x = text_of(html)
    if "ご予約いただけるプランがございません" in x:
        return []
    hits = []
    # プランごとに分割（件数表示の後ろから）
    for blk in re.split(r'class="plan-list webc_box"', html)[1:]:
        bt = text_of("<div " + blk)
        tm = re.search(r'webc_box_head.*?<span>(.*?)</span>', blk, re.S)
        title = re.search(r"(.+)", text_of(tm.group(1)).strip()) if tm else None
        meal = re.search(r"お食事 (\S+)", bt)
        price = re.search(r"([\d,]+)円 ～", bt)
        meal_s = meal.group(1) if meal else "?"
        hits.append({
            "key": f"489:{slug}:{(title.group(1) if title else bt[:40])[:60]}",
            "hotel": name,
            "plan": (title.group(1) if title else bt[:60]).strip(),
            "meal": meal_s,
            "dinner": "夕" in meal_s or "2食" in meal_s or "二食" in meal_s,
            "price": (price.group(1) + "円〜/人") if price else "",
            "url": page_url,
        })
    return hits


# ---------- 楽天 ----------
RAKUTEN_ROOM = re.compile(
    r"食事 (朝食あり|朝食なし) (夕食あり|夕食なし) 人数.*?キャンセル "
    r"(空室なし|大人\d+人 ／1泊の料金.*?合計 ([\d,]+) 円)"
)


def check_rakuten(no: str, name: str) -> list[dict]:
    url = (f"https://hotel.travel.rakuten.co.jp/hotelinfo/plan/{no}"
           f"?f_nen1={d_in.year}&f_tuki1={d_in.month}&f_hi1={d_in.day}"
           f"&f_nen2={d_out.year}&f_tuki2={d_out.month}&f_hi2={d_out.day}"
           f"&f_heya_su=1&f_otona_su={ADULTS}")
    r = session().get(url, timeout=30)
    r.raise_for_status()
    x = text_of(r.text)
    if "宿泊プラン" not in x:
        raise RuntimeError("unexpected page")
    hits = []
    for i, m in enumerate(RAKUTEN_ROOM.finditer(x)):
        if m.group(3) == "空室なし":
            continue
        hits.append({
            "key": f"rk:{no}:{m.group(1)}{m.group(2)}:{m.group(4)}",
            "hotel": name,
            "plan": "楽天トラベル",
            "meal": f"{m.group(1)}・{m.group(2)}",
            "dinner": m.group(2) == "夕食あり",
            "price": f"2名合計 {m.group(4)}円",
            "url": url,
        })
    return hits


# ---------- じゃらん ----------
def check_jalan(yad: str, name: str) -> list[dict]:
    url = (f"https://www.jalan.net/yad{yad}/plan/?stayYear={d_in.year}"
           f"&stayMonth={d_in.month}&stayDay={d_in.day}&stayCount=1"
           f"&roomCount=1&adultNum={ADULTS}")
    r = session().get(url, timeout=30)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "cp932"
    x = text_of(r.text)
    if "料金・宿泊プラン" not in x:
        raise RuntimeError("unexpected page")
    if "ご利用できるプランがない" in x:
        return []
    # クラノバは全プラン夕食付き（オーベルジュ）なので空きが出たら通知
    return [{
        "key": f"jl:{yad}",
        "hotel": name,
        "plan": "じゃらん",
        "meal": "要確認",
        "dinner": True,
        "price": "",
        "url": url,
    }]


# ---------- 通知 ----------
def notify(text: str) -> None:
    print(text)
    if not WEBHOOK:
        return
    for i in range(0, len(text), 1900):
        requests.post(WEBHOOK, json={"content": text[i:i + 1900]}, timeout=15)


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"seen": [], "errors": {}}


def save_state(st: dict) -> None:
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1))


def run_once(st: dict) -> None:
    now = datetime.now(JST).strftime("%m/%d %H:%M")
    found = []
    jobs = ([(check_489ban, k, v) for k, v in BAN489.items()]
            + [(check_rakuten, k, v) for k, v in RAKUTEN.items()]
            + [(check_jalan, k, v) for k, v in JALAN.items()])
    for fn, k, v in jobs:
        tag = f"{fn.__name__}:{k}"
        try:
            found += fn(k, v)
            st["errors"].pop(tag, None)
        except Exception as e:  # noqa: BLE001
            n = st["errors"].get(tag, 0) + 1
            st["errors"][tag] = n
            print(f"[{now}] ERROR {v} ({tag}): {e}", file=sys.stderr)
            if n == 10:  # 10回連続で失敗したら1度だけ知らせる
                notify(f"⚠️ {v} の取得に10回連続で失敗しています（{tag}）。ページ構造が変わったかも。")
        time.sleep(2)

    if DINNER_ONLY:
        found = [h for h in found if h["dinner"]]
    cur = {h["key"] for h in found}
    new = [h for h in found if h["key"] not in st["seen"]]
    st["seen"] = sorted(cur)  # 埋まったら消える → 再び空けばまた通知

    print(f"[{now}] 空き {len(found)}件 / 新規 {len(new)}件")
    if new:
        lines = [f" 🎉 **銀山温泉 {d_in:%m/%d} 泊・{ADULTS}名 に空きが出ました！**"]
        for h in new:
            lines.append(f"・**{h['hotel']}**｜{h['meal']}｜{h['price']}\n　{h['plan']}\n　{h['url']}")
        lines.append(f"（{now} 検知）")
        notify("\n".join(lines))


def main() -> None:
    if date.today() > d_in:
        print("宿泊日を過ぎたので終了")
        return
    st = load_state()
    end = time.time() + LOOP_MINUTES * 60
    while True:
        run_once(st)
        save_state(st)
        if time.time() + INTERVAL_SEC > end:
            break
        time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    main()
