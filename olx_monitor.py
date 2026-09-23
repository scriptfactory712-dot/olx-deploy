#!/usr/bin/env python3
"""
Auto Monitor OLX.ua - машины Днепропетровская обл + Самар, $500-$4000
Только свежие (сегодня), старье не шлет.
Работает на Termux ночью, шлет новые объявления в Telegram.
Только stdlib + requests.
"""
import os, re, json, time, sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import date, datetime, timedelta
import urllib.parse
try:
    import requests
except ImportError:
    print("Нужно: pip install requests")
    sys.exit(1)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()

# Цены НЕ фильтруем: ищем ВСЕ легковые Самар + Днепр. обл
UAH_FROM = int(os.environ.get("UAH_FROM", "0"))
UAH_TO = int(os.environ.get("UAH_TO", "0"))
# Финальный фильтр в USD (0..inf = без ограничения)
USD_MIN = float(os.environ.get("USD_MIN", "0"))
USD_MAX = float(os.environ.get("USD_MAX", "999999999"))
USD_RATE = float(os.environ.get("USD_RATE", "42.5"))  # грн за $1
EUR_RATE = float(os.environ.get("EUR_RATE", "45.5"))

CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "60"))  # 1 мин
HEARTBEAT_EVERY = int(os.environ.get("HEARTBEAT_EVERY", "1"))  # писать «ищу» каждую проверку
PAGES = int(os.environ.get("PAGES", "2"))  # проверять 2 страницы выдачи (глубже)
JUICY_TOP = int(os.environ.get("JUICY_TOP", "5"))  # слать только топ-N самых сочных новинок

# Мусор: не легковые / спам. Такие молча пропускаем.
JUNK_WORDS = [
    "прицеп", "лафет", "мото", "запчаст", "шин", "диск",
    "грузовик", "грузовой", "автобус", "спецтехн", "сельхоз",
    "газель", "соболь", "транзит", "transit",
    "продаж - обмен - рассрочка", "продам документы", "документы ",
    " Touareg - ".lower() + "2007", "документы touareg",
]

def is_junk(title):
    t = " " + title.lower() + " "
    for w in JUNK_WORDS:
        if w in t:
            return True
    # спам-заглушки дилеров без названия машины
    if len(title.strip()) < 10:
        return True
    return False

def juicy_key(item):
    a, usd = item
    price = usd if usd and usd > 0 else float("inf")  # без цены — в конец
    return (0 if is_samar(a["loc"]) else 1, price)  # сначала Самар, потом дешевле

SEARCH_URL = (
    "https://www.olx.ua/uk/transport/legkovye-avtomobili/dnp/"
    "?search%5Border%5D=created_at%3Adesc"
)

SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept-Language": "uk-UA,uk;q=0.9,ru;q=0.8",
}

def start_health_server():
    """Пустышка для Render Web Service Free: отвечает 200 OK, чтобы не ругался.
    UptimeRobot пингует ее каждые 5 мин и не дает уснуть."""
    port = int(os.environ.get("PORT", "10000"))

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK - OLX monitor alive")
        def log_message(self, *a):
            pass

    try:
        srv = ThreadingHTTPServer(("0.0.0.0", port), H)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        print(f"Health server on :{port}", flush=True)
    except Exception as e:
        print("Health server err:", e, flush=True)

def load_seen():
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_seen(s):
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(list(s))[-2000:], f)
    except Exception as e:
        print("save_seen err:", e)

def send_tg(text):
    if not BOT_TOKEN or not CHAT_ID:
        print("Нет BOT_TOKEN/CHAT_ID, пропуск отправки")
        print(text[:200])
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": False},
            timeout=20,
        )
        if r.status_code != 200:
            print("TG err:", r.status_code, r.text[:300])
            return False
        return True
    except Exception as e:
        print("TG exception:", e)
        return False

def parse_price_to_usd(price_str):
    """'121 002.34 грн.' / '2 500 $' / 'Договорная' -> usd float или None"""
    if not price_str:
        return None
    s = price_str.strip()
    low = s.lower()
    if "догов" in low or "безкоштовно" in low or "бесплатно" in low or "обмін" in low or "обмен" in low:
        return None
    # число
    m = re.search(r"([\d\s\u00a0\.,]+)", s)
    if not m:
        return None
    num_s = m.group(1).replace(" ", "").replace("\u00a0", "").replace(",", ".")
    # убрать лишние точки (121.002.34 -> 121002.34)
    parts = num_s.split(".")
    if len(parts) > 2:
        num_s = "".join(parts[:-1]) + "." + parts[-1]
    try:
        val = float(num_s)
    except Exception:
        return None
    if "$" in s or "дол" in low or "usd" in low:
        return val
    if "€" in s or "євро" in low or "евро" in low or "eur" in low:
        return val * EUR_RATE / USD_RATE
    # по умолчанию грн
    return val / USD_RATE

def fetch_html(url):
    # requests всегда дает 403 на OLX — идем сразу в curl (быстрее на 1-2с)
    import subprocess
    cmd = ["curl", "-Ls", "--max-time", "20", "--connect-timeout", "10",
           "-A", HEADERS["User-Agent"],
           "-H", "Accept-Language: uk-UA,uk;q=0.9", url]
    out = subprocess.check_output(cmd, timeout=25)
    return out.decode("utf-8", errors="ignore")

def fetch_ads(pages=None):
    all_ads = []
    if pages is None:
        pages = PAGES
    for page in range(1, pages + 1):
        url = SEARCH_URL if page == 1 else (SEARCH_URL + f"&page={page}")
        try:
            html = fetch_html(url)
        except Exception as e:
            print(f"page {page} fetch err: {e}", flush=True)
            continue
        # режем карточки
        cards = re.split(r'<div data-cy="l-card"', html)[1:]
        for c in cards:
            c_clean = re.sub(r"<style.*?</style>", "", c, flags=re.S)
            m_link = re.search(r'href="(/d/uk/obyavlenie/[^"]+\.html[^"]*)"', c_clean)
            if not m_link:
                continue
            full_href = m_link.group(1)
            if "promoted" in full_href or "extended_search" in full_href:
                continue  # реклама/спонсорка и заглушки — пропускаем, только органика
            path = full_href.split("?")[0].split("&")[0]
            link = "https://www.olx.ua" + path
            # ID из URL ...-IDxxxxx.html
            m_id = re.search(r"-(ID[A-Za-z0-9]+)\.html", path)
            ad_id = m_id.group(1) if m_id else path
            m_title = re.search(r"<h4[^>]*>(.*?)</h4>", c_clean, re.S)
            title = re.sub(r"<[^>]+>", "", m_title.group(1)).strip() if m_title else "Без назви"
            m_price = re.search(r'<p data-testid="ad-price"[^>]*>([^<]+)', c_clean, re.S)
            price_raw = m_price.group(1).strip() if m_price else ""
            m_loc = re.search(r'<p data-testid="location-date"[^>]*>(.*?)</p>', c_clean, re.S)
            loc = re.sub(r"<[^>]+>", "", m_loc.group(1)).strip() if m_loc else ""
            all_ads.append({"id": ad_id, "title": title, "price": price_raw, "loc": loc, "url": link})
    # уберем дубли по id, сохраним порядок
    uniq, seen = [], set()
    for a in all_ads:
        if a["id"] not in seen and "extended_search" not in a["url"]:
            seen.add(a["id"])
            uniq.append(a)
    return uniq

def is_samar(loc):
    l = loc.lower()
    return "самар" in l or "samar" in l or "новомосков" in l or "novomoskov" in l

UK_MONTHS = {
    "січня": 1, "лютого": 2, "березня": 3, "квітня": 4, "травня": 5, "червня": 6,
    "липня": 7, "серпня": 8, "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12,
    # русские варианты на всякий
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}

def parse_ad_date(loc):
    """'Самар - Сьогодні о 14:27' -> date.today(); 'Дніпро - 20 вересня 2026 р.' -> date(2026,9,20); иначе None"""
    if not loc:
        return None
    l = loc.lower()
    today = date.today()
    if "сьогодні" in l or "сегодня" in l or "today" in l:
        return today
    if "вчора" in l or "вчера" in l or "yesterday" in l:
        return today - timedelta(days=1)
    m = re.search(r"(\d{1,2})\s+([а-яіїєa-z]+)", l)
    if m:
        try:
            d = int(m.group(1))
            mon = UK_MONTHS.get(m.group(2), None)
            if mon:
                y = today.year
                my = re.search(r"(20\d{2})", l)
                if my:
                    y = int(my.group(1))
                return date(y, mon, d)
        except Exception:
            return None
    return None

def is_fresh(loc):
    """Только свежие: сегодня (Сьогодні). Старье типа 20 вересня — False."""
    d = parse_ad_date(loc)
    if d is None:
        return True  # дату не распознали — не режем, решает seen-фильтр
    return d >= date.today()

def format_msg(a, usd):
    tag = "⭐ САМАР!" if is_samar(a["loc"]) else "🚗 Новое авто"
    usd_s = f" (~${usd:.0f})" if usd else ""
    seen_at = time.strftime("%H:%M")
    return f"{tag}\n{a['title']}\n💰 {a['price']}{usd_s}\n📍 {a['loc']}\n👁 Заметил в {seen_at}\n🔗 {a['url']}"

def main():
    start_health_server()
    print("URL:", SEARCH_URL, flush=True)
    print(f"Фильтр: ВСЕ легковые, без ограничения цены, каждые {CHECK_INTERVAL}с", flush=True)
    if not BOT_TOKEN or not CHAT_ID:
        print("ВНИМАНИЕ: задай BOT_TOKEN и CHAT_ID через env!", flush=True)
    seen = load_seen()
    first = len(seen) == 0
    print(f"seen: {len(seen)}, first_run={first}", flush=True)
    if first:
        try:
            ads = fetch_ads()
            print(f"Первый запуск: найдено {len(ads)}", flush=True)
            for a in ads:
                seen.add(a["id"])
            save_seen(seen)
            send_tg(f"✅ Авто-монитор OLX запущен!\nОбласть: Днепропетровская\nГород: Самар + область\nВсе легковые, без ограничения цены\nНайдено сейчас: {len(ads)}\nПроверка каждые {CHECK_INTERVAL} сек.")
        except Exception as e:
            print("Первый fetch err:", e, flush=True)
    check_no = [0]
    fails = 0
    FULL_SWEEP_EVERY = int(os.environ.get("FULL_SWEEP_EVERY", "10"))  # каждую 10-ю проверку — все страницы
    while True:
        fresh, skipped_old, skipped_junk, skipped_extra = [], 0, 0, 0
        try:
            check_no[0] += 1
            # новинки всегда на стр.1 -> ее каждую проверку; глубокий проход реже (меньше нагрузка = меньше бан)
            pages_now = PAGES if (check_no[0] % FULL_SWEEP_EVERY == 0) else 1
            ads = fetch_ads(pages_now)
            fails = 0
            for a in ads:
                if a["id"] not in seen:
                    # 0) мусор: не легковые / спам — молча в seen
                    if is_junk(a["title"]):
                        seen.add(a["id"])
                        skipped_junk += 1
                        continue
                    # 1) дата: старье (не сегодня) — молча в seen, не шлем
                    if not is_fresh(a["loc"]):
                        seen.add(a["id"])
                        skipped_old += 1
                        continue
                    usd = parse_price_to_usd(a["price"])
                    # без цены (договорная): шлем тоже, т.к. фильтр по цене снят
                    if usd is None:
                        if USD_MIN <= 0:
                            fresh.append((a, 0))
                        else:
                            seen.add(a["id"])
                        continue
                    if USD_MIN <= usd <= USD_MAX:
                        fresh.append((a, usd))
                    else:
                        seen.add(a["id"])
            # свежие снизу вверх (сначала старые из новых), чтобы порядок был хронологический
            # сочность: Самар + дешевле — в топ, шлем только топ-N
            fresh = sorted(fresh, key=juicy_key)
            juicy, extra = fresh[:JUICY_TOP], fresh[JUICY_TOP:]
            for a, usd in extra:
                seen.add(a["id"])
            skipped_extra = len(extra)
            for i, (a, usd) in enumerate(juicy):
                msg = format_msg(a, usd)
                if i == 0:
                    msg = "🔥 САМАЯ СОЧНАЯ\n" + msg
                ok = send_tg(msg)
                print(("SENT " if ok else "FAIL ") + a["id"] + " " + a["title"][:60], flush=True)
                seen.add(a["id"])
                time.sleep(1)
            if fresh:
                save_seen(seen)
            else:
                # периодически сохраняем даже без новых (на случай перезапуска)
                save_seen(seen)
            print(f"[{time.strftime('%H:%M:%S')}] проверено: {len(ads)}, сочных: {len(juicy)}/{len(fresh)}, мусор: {skipped_junk}, старье: {skipped_old}, вне топа: {skipped_extra}", flush=True)
            # heartbeat каждую проверку: «все еще ищу»
            if HEARTBEAT_EVERY > 0 and check_no[0] % HEARTBEAT_EVERY == 0:
                hb = (f"🔍 Все еще ищу... ({time.strftime('%H:%M:%S')})\n"
                      f"Проверено: {len(ads)} | новинок: {len(fresh)}, сочных отправлено: {len(juicy)}\n"
                      f"Все легковые, Самар + Днепр. обл, без ограничения цены")
                send_tg(hb)
        except Exception as e:
            fails += 1
            print(f"[{time.strftime('%H:%M:%S')}] ошибка: {e} (подряд: {fails})", flush=True)
            if fails == 5:
                send_tg("⚠️ OLX не отвечает уже 5 проверок подряд. Мониторю дальше, как отпустит — пришлю новинки.")
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
