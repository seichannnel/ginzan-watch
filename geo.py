"""ゲオモバイル 訳アリ品アウトレット 在庫監視 → Discord通知"""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from curl_cffi import requests as cffi  # ブラウザの通信を再現（Akamai対策）

URL = "https://mvno.geo-mobile.jp/outlet/"
WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "")
KEYWORDS = [k.strip() for k in os.getenv("GEO_KEYWORDS", "").split(",") if k.strip()]  # 空なら全機種
LOOP_MINUTES = float(os.getenv("LOOP_MINUTES", "0"))
INTERVAL_SEC = int(os.getenv("INTERVAL_SEC", "60"))
STATE_FILE = Path("geo_state.json")
JST = timezone(timedelta(hours=9))
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
}


def clean(s: str) -> str:
    s = re.sub(r"<br\s*/?>", " ", s)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", s).strip()


def parse(html: str) -> list[dict]:
    items = []
    for blk in html.split('class="item_text"')[1:]:
        h3 = re.search(r"<h3>(.*?)</h3>", blk, re.S)
        if not h3:
            continue
        name = clean(h3.group(1)).replace("【状態C（訳アリ品）】", "").strip()
        rows = re.findall(
            r'class="production_strage">\s*<div>(.*?)</div>\s*<strong>(.*?)</strong>.*?'
            r'class="production_mode">(.*?)(?=class="production_strage"|</dd>)',
            blk, re.S)
        for cap, price, mode in rows:
            btn = re.search(r'class="apply-btn([^"]*)"', mode)
            items.append({
                "name": name,
                "cap": clean(cap),
                "price": clean(price),
                "in_stock": bool(btn) and "-soldout" not in btn.group(1),
            })
    return items


def notify(text: str) -> None:
    print(text)
    if WEBHOOK:
        requests.post(WEBHOOK, json={"content": text[:1990]}, timeout=15)


def run_once(st: dict) -> None:
    now = datetime.now(JST).strftime("%m/%d %H:%M")
    try:
        r = None
        for imp in ("chrome", "chrome131", "chrome120"):
            r = cffi.get(URL, impersonate=imp, timeout=30,
                         headers={"Accept-Language": HEADERS["Accept-Language"]})
            if r.status_code == 200:
                break
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}（アクセス拒否）")
        items = parse(r.text)
        if not items:
            raise RuntimeError("商品が1件も読み取れない（ページ構造が変わった可能性）")
        st["errors"] = 0
    except Exception as e:  # noqa: BLE001
        st["errors"] = st.get("errors", 0) + 1
        print(f"[{now}] ERROR: {e}", file=sys.stderr)
        if st["errors"] == 10:
            notify(f"⚠️ ゲオアウトレットの取得に10回連続で失敗しています：{e}")
        return

    if KEYWORDS:
        items = [i for i in items if any(k.lower() in i["name"].lower() for k in KEYWORDS)]
    stock = [i for i in items if i["in_stock"]]
    cur = {f"{i['name']}|{i['cap']}" for i in stock}
    new = [i for i in stock if f"{i['name']}|{i['cap']}" not in st.get("seen", [])]
    st["seen"] = sorted(cur)
    print(f"[{now}] 監視 {len(items)}件 / 在庫あり {len(stock)}件 / 新規 {len(new)}件")
    if new:
        lines = ["📱 **ゲオ アウトレットに在庫が入りました！**"]
        for i in new:
            cap = f" {i['cap']}" if i["cap"] else ""
            lines.append(f"・**{i['name']}{cap}**｜{i['price']}")
        lines.append(URL)
        lines.append(f"（{now} 検知）")
        notify("\n".join(lines))


def main() -> None:
    st = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    end = time.time() + LOOP_MINUTES * 60
    while True:
        run_once(st)
        STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1))
        if time.time() + INTERVAL_SEC > end:
            break
        time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    main()
