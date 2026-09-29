"""
Detail-парсер: bellesdemeures.com + seloger.com + selogerneuf.com
через РЕАЛЬНЫЙ Chrome + Playwright (CDP).

Скорость: ~8-12 сек на объявление (в основном пауза между).
"""

import os
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")

import json
import math
import random
import re
import shutil
import signal
import subprocess
import time
import traceback
import urllib.request

from load_django import *
from parser_app.models import Product

from playwright.sync_api import sync_playwright


# ===========================================================================
# НАСТРОЙКИ
# ===========================================================================
CDP_PORT = 9222
CDP_URL = f"http://localhost:{CDP_PORT}"
USER_DATA_DIR = os.path.expanduser("~/seloger_chrome_profile")

CHROME_START_TIMEOUT = 20
NAV_TIMEOUT = 30_000               # макс. ожидание навигации (реально ~1с)
DELAY_BETWEEN_ITEMS = (4, 7)       # пауза между объявлениями
MOUSE_SPEED = 12                   # быстрее движения мыши
POST_NAV_SLEEP = 0.4               # ожидание после goto

DD_WAIT_MAX = 25                   # макс. ожидание DataDome
DD_CHECK_INTERVAL = 0.5
DD_RELOAD_EVERY = 8

# Full human behavior каждые N объявлений, остальное — light
HUMAN_FULL_EVERY = 10

_ITEM_COUNTER = 0

CHROME_CANDIDATES = [
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/snap/bin/chromium",
]


# ===========================================================================
# CHROME
# ===========================================================================

def find_chrome():
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        p = shutil.which(name)
        if p:
            return p
    for p in CHROME_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def cdp_version():
    try:
        with urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=2) as r:
            return json.loads(r.read()).get("Browser")
    except Exception:
        return None


def kill_our_chrome():
    try:
        out = subprocess.check_output(["ps", "-eo", "pid,args"], text=True)
    except Exception:
        return
    pids = []
    for line in out.splitlines():
        if USER_DATA_DIR in line and "chrome" in line.lower():
            try:
                pids.append(int(line.strip().split()[0]))
            except Exception:
                pass
    if not pids:
        return
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
    for _ in range(8):
        alive = []
        for pid in pids:
            try:
                os.kill(pid, 0)
                alive.append(pid)
            except OSError:
                pass
        if not alive:
            break
        time.sleep(0.4)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
    print(f"[CHROME] убито процессов: {len(pids)}")
    time.sleep(0.5)


def launch_chrome(chrome_path):
    os.makedirs(USER_DATA_DIR, exist_ok=True)
    print(f"[CHROME] запуск: {chrome_path}")
    proc = subprocess.Popen(
        [
            chrome_path,
            f"--remote-debugging-port={CDP_PORT}",
            f"--user-data-dir={USER_DATA_DIR}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
            "--start-maximized",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"[CHROME] PID: {proc.pid}")
    return proc


def wait_for_cdp(timeout=CHROME_START_TIMEOUT):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = cdp_version()
        if v:
            print(f"[CHROME] CDP готов: {v}")
            return True
        time.sleep(0.3)
    return False


# ===========================================================================
# DATADOME
# ===========================================================================

def datadome_trigger(page, html):
    if "geo.captcha-delivery.com/captcha" in html:
        return "captcha-iframe"
    if "DataDome CAPTCHA" in html:
        return "DataDome CAPTCHA title"
    try:
        if "captcha-delivery.com" in page.url:
            return "url captcha-delivery"
    except Exception:
        pass
    try:
        title = (page.title() or "").strip().lower()
        if title in ("bellesdemeures.com", "seloger.com", "selogerneuf.com"):
            return "title is domain"
    except Exception:
        pass
    return None


def wait_for_datadome(page, max_wait=DD_WAIT_MAX):
    deadline = time.time() + max_wait
    started = time.time()
    print(f"   [DD]         жду челлендж (до {max_wait}s)…")
    last_log = -1
    last_reload = 0

    while time.time() < deadline:
        try:
            html = page.content()
        except Exception:
            time.sleep(DD_CHECK_INTERVAL)
            continue

        reason = datadome_trigger(page, html)
        now = time.time() - started

        if reason is None:
            print(f"   [DD]         пройден за {now:.1f}s")
            return True

        if int(now) - last_log >= 3:
            print(f"   [DD]         t={int(now)}s | {reason}")
            last_log = int(now)

        if int(now) - last_reload >= DD_RELOAD_EVERY:
            print(f"   [DD]         page.reload()")
            try:
                page.reload(wait_until="domcontentloaded", timeout=20000)
            except Exception:
                pass
            last_reload = int(now)

        time.sleep(DD_CHECK_INTERVAL)

    print(f"   [DD]         НЕ пройден за {max_wait}s")
    return False


# ===========================================================================
# HUMAN-LIKE
# ===========================================================================

def wind_mouse(page, sx, sy, ex, ey,
               gravity=9.0, wind=3.0, max_step=15.0, target_area=10.0):
    sqrt3, sqrt5 = math.sqrt(3), math.sqrt(5)
    cx, cy = float(sx), float(sy)
    vx = vy = wx = wy = 0.0
    while math.hypot(ex - cx, ey - cy) > 1.0:
        dist = math.hypot(ex - cx, ey - cy)
        wx = wx / sqrt3 + (random.random() * 2 - 1) * wind / sqrt5
        wy = wy / sqrt3 + (random.random() * 2 - 1) * wind / sqrt5
        if dist >= target_area:
            gx = gravity * (ex - cx) / dist
            gy = gravity * (ey - cy) / dist
        else:
            gx = gy = 0.0
        vx += wx + gx
        vy += wy + gy
        vm = math.hypot(vx, vy)
        if vm > max_step:
            r = max_step / 2 + random.random() * max_step / 2
            vx = vx / vm * r
            vy = vy / vm * r
        cx += vx
        cy += vy
        try:
            page.mouse.move(cx, cy)
        except Exception:
            return
        time.sleep(1.0 / MOUSE_SPEED * (0.3 + random.random() * 0.5))


def human_scroll(page, total_px):
    d = 1 if total_px > 0 else -1
    total = abs(total_px)
    done = 0
    while done < total:
        step = min(random.randint(100, 200), total - done)
        try:
            page.mouse.wheel(0, d * step)
        except Exception:
            return
        done += step
        time.sleep(random.uniform(0.04, 0.15))


def human_behavior(page, intensity="light"):
    """
    light: ~0.3-0.5 сек — быстрый wiggle мыши
    full:  ~3-5 сек   — wiggle + scroll + wiggle
    """
    vp = page.viewport_size or {"width": 1366, "height": 900}
    w, h = vp["width"], vp["height"]

    if intensity == "light":
        wind_mouse(page,
                   w // 2, h // 2,
                   w // 2 + random.randint(-150, 150),
                   h // 2 + random.randint(-100, 100))
        return

    # full
    wind_mouse(page, random.randint(0, 80), random.randint(0, 80),
               w // 2 + random.randint(-250, 250), h // 2 + random.randint(-200, 200))
    time.sleep(random.uniform(0.1, 0.3))
    human_scroll(page, random.randint(300, 700))
    time.sleep(random.uniform(0.2, 0.5))
    human_scroll(page, -random.randint(100, 200))


# ===========================================================================
# УТИЛИТЫ
# ===========================================================================

def clean(v):
    if v is None:
        return None
    return re.sub(r"\s+", " ", str(v)).strip() or None


def fmt_price(v):
    if v is None:
        return None
    try:
        return f"{int(v):,}".replace(",", " ") + " €"
    except Exception:
        return str(v)


def fmt_price_m2(v):
    if v is None:
        return None
    try:
        return f"{int(round(v)):,}".replace(",", " ") + " €/m²"
    except Exception:
        return str(v)


def detect_domain(url):
    if not url:
        return "unknown"
    if "bellesdemeures.com" in url:
        return "bellesdemeures"
    if "selogerneuf.com" in url:
        return "selogerneuf"
    if "seloger.com" in url:
        return "seloger"
    return "unknown"


def _log_field(name, value, limit=200):
    if value is None:
        print(f"   [FIELD] {name:12}: (None)")
    elif isinstance(value, str):
        v = value if len(value) <= limit else value[:limit] + "…"
        print(f"   [FIELD] {name:12}: {v!r}")
    else:
        print(f"   [FIELD] {name:12}: {value!r}")


# ===========================================================================
# ПАРСЕР BELLESDEMEURES
# ===========================================================================

def parse_bellesdemeures(page, html):
    r = {k: None for k in ("title", "surface", "type", "rooms",
                           "description", "price", "price_m2", "location")}

    def _t(sel):
        try:
            return clean(page.locator(sel).first.inner_text(timeout=2000))
        except Exception:
            return None

    r["type"] = _t("span.detailBannerInfosTypeBien")
    r["price"] = _t("span.js_price")
    r["location"] = _t("div.annonceSpecsListItemVille")
    r["description"] = _t("p.detailDescSummary")

    rooms = _t("li.annonceSpecsListItem.js_nbRooms span")
    if rooms:
        m = re.match(r"^(\d+)", rooms)
        r["rooms"] = m.group(1) if m else rooms

    r["surface"] = _t("li.annonceSpecsListItem.js_livingArea span")

    if not r["type"]:
        h1 = _t("h1.title")
        if h1:
            mm = re.match(r"^\s*([A-Za-zÀ-ÿ]+)", h1)
            if mm:
                r["type"] = mm.group(1)

    r["title"] = clean(" • ".join([p for p in [r["type"], r["location"]] if p]))

    _log_field("type", r["type"])
    _log_field("title", r["title"])
    _log_field("rooms", r["rooms"])
    _log_field("surface", r["surface"])
    _log_field("price", r["price"])
    _log_field("location", r["location"])
    return r


# ===========================================================================
# ПАРСЕР SELOGER
# ===========================================================================

def _fill_from_seloger_json(classified, r):
    s = classified.get("sections", {})
    hard = s.get("hardFacts", {}) or {}
    desc = s.get("description", {}) or {}
    loc = (s.get("location", {}) or {}).get("address", {}) or {}

    title_raw = hard.get("title") or ""
    r["type"] = clean(re.sub(r"\s+(à\s+\w+|à\s+vendre|à\s+louer)$", "",
                             title_raw, flags=re.I))
    r["title"] = clean(desc.get("headline") or title_raw)

    for f in hard.get("facts", []) or []:
        t, v = f.get("type"), f.get("value")
        if t == "livingSpace":
            r["surface"] = clean(v)
        elif t == "numberOfRooms":
            r["rooms"] = clean(v)

    r["description"] = clean(desc.get("description"))
    p = hard.get("price", {}) or {}
    r["price"] = clean(p.get("value") or p.get("formatted"))

    ps = s.get("price", {}) or {}
    for comp in ps.get("components", []) or []:
        for u in comp.get("units", []) or []:
            mv = u.get("main", {}).get("price", {}).get("value", {}) or {}
            alt = (mv.get("alt") or {}).get("value")
            if alt and "m²" in alt:
                r["price_m2"] = clean(alt)
                break

    parts = [loc.get("district"), loc.get("city")]
    if loc.get("zipCode"):
        parts.append(f"({loc['zipCode']})")
    r["location"] = clean(", ".join(filter(None, parts)))


def parse_seloger(page, html):
    """
    Достаём JSON из HTML через regex (быстрее и надёжнее, чем через locator).
    Внутри HTML есть строка:
        window["__UFRN_LIFECYCLE_SERVERREQUEST__"]=JSON.parse("{\"data\":{...}}");
    Мы её вытаскиваем и правильно декодируем (без порчи UTF-8).
    """
    r = {k: None for k in ("title", "surface", "type", "rooms",
                           "description", "price", "price_m2", "location")}

    m = re.search(
        r'window\["__UFRN_LIFECYCLE_SERVERREQUEST__"\]\s*=\s*JSON\.parse\("(.*?)"\)\s*;',
        html, re.S)
    if not m:
        print(f"   [SL-JSON]    __UFRN_LIFECYCLE_SERVERREQUEST__ не найден")
        return r

    try:
        # Оборачиваем в кавычки и парсим как JSON-строку — так корректно
        # раскрываются escape-последовательности (\" \\ \uXXXX) БЕЗ порчи UTF-8
        inner_str = json.loads('"' + m.group(1) + '"')
        data = json.loads(inner_str)
        classified = data["app_cldp"]["data"]["classified"]

        sections = classified.get("sections", {})
        print(f"   [SL-JSON]    sections: {list(sections.keys())}")
        hard = sections.get("hardFacts", {}) or {}
        facts = hard.get("facts", []) or []
        print(f"   [SL-JSON]    facts: {[f.get('type') for f in facts]}")

        _fill_from_seloger_json(classified, r)

        if r["title"] or r["description"]:
            print("   [SL-JSON]    ok")
            _log_field("type", r["type"])
            _log_field("title", r["title"])
            _log_field("rooms", r["rooms"])
            _log_field("surface", r["surface"])
            _log_field("price", r["price"])
            _log_field("price_m2", r["price_m2"])
            _log_field("location", r["location"])
            _log_field("description", r["description"], limit=150)
            return r
    except Exception as e:
        print(f"   [SL-JSON]    parse fail: {type(e).__name__}: {e}")

    return r


# ===========================================================================
# ПАРСЕР SELOGERNEUF
# ===========================================================================

def parse_selogerneuf(page, html):
    r = {k: None for k in ("title", "surface", "type", "rooms",
                           "description", "price", "price_m2", "location")}

    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        print(f"   [SLN-JSON]   __NEXT_DATA__ не найден")
        return r

    try:
        data = json.loads(m.group(1))
        annonce = data["props"]["initialReduxState"]["detailsAnnonce"]["annonce"]
    except (KeyError, TypeError, json.JSONDecodeError) as e:
        print(f"   [SLN-JSON]   fail: {type(e).__name__}: {e}")
        return r

    r["title"] = clean(annonce.get("name"))
    r["description"] = clean(annonce.get("description"))
    r["type"] = "Programme neuf"

    loc = annonce.get("location", {}) or {}
    addr = loc.get("address")
    city_obj = loc.get("city", {}) or {}
    city_name = city_obj.get("cityName")
    zip_code = city_obj.get("zipCode")
    parts = [p for p in [addr, city_name, f"({zip_code})" if zip_code else None] if p]
    r["location"] = clean(", ".join(parts)) if parts else None

    pts = annonce.get("propertyTypes", []) or []
    if pts:
        pt = pts[0]
        price_min = pt.get("priceMin") or pt.get("priceMax")
        if price_min:
            r["price"] = fmt_price(price_min)

        props = pt.get("propertiesByType", []) or []
        if props:
            p0 = props[0]
            rooms = p0.get("rooms")
            area = p0.get("livingArea") or p0.get("usableLivingArea")
            price_unit = p0.get("price") or price_min

            if rooms:
                r["rooms"] = str(rooms)
            if area:
                r["surface"] = f"{area} m²"
                if price_unit:
                    r["price_m2"] = fmt_price_m2(price_unit / area)

    print("   [SLN-JSON]   ok")
    _log_field("type", r["type"])
    _log_field("title", r["title"])
    _log_field("rooms", r["rooms"])
    _log_field("surface", r["surface"])
    _log_field("price", r["price"])
    _log_field("location", r["location"])
    return r


# ===========================================================================
# ПАРСИНГ ОДНОГО ОБЪЯВЛЕНИЯ
# ===========================================================================

def parse_product(page, item):
    global _ITEM_COUNTER
    _ITEM_COUNTER += 1

    t = time.monotonic()
    print(f"\n[START] id={item.id}")
    print(f"   [LINK]       {item.link}")

    if not item.link:
        print(f"   [SKIP]       пустой link")
        return None

    domain = detect_domain(item.link)
    print(f"   [DOMAIN]     {domain}")

    if domain == "unknown":
        print(f"   [SKIP]       неизвестный домен")
        return None

    # --- Навигация ---
    try:
        response = page.goto(item.link, timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
        print(f"   [NAV]        {time.monotonic() - t:.2f}s")
    except Exception as e:
        msg = str(e).lower()
        if "connection closed" in msg or "target closed" in msg or "browser has been closed" in msg:
            print(f"   [BROWSER-DEAD] {e}")
            raise
        print(f"   [NAV-ERR]    {type(e).__name__}: {e}")
        return "Failed"

    if response is not None and response.status >= 400:
        print(f"   [HTTP-ERR]   {response.status}")
        return "Failed"

    # короткая пауза для React
    time.sleep(POST_NAV_SLEEP)

    try:
        print(f"   [TITLE]      {(page.title() or '')[:110]}")
    except Exception:
        pass

    html = page.content()
    print(f"   [HTML-LEN]   {len(html)}")

    # --- DataDome ---
    if datadome_trigger(page, html):
        if not wait_for_datadome(page, max_wait=DD_WAIT_MAX):
            print(f"   [CAPTCHA]    DataDome не прошёл")
            return "Captcha"
        html = page.content()
        print(f"   [HTML-LEN]   {len(html)} (после DD)")

    # --- Поведение ---
    if _ITEM_COUNTER % HUMAN_FULL_EVERY == 0:
        print(f"   [HUMAN]      full")
        human_behavior(page, intensity="full")
    else:
        human_behavior(page, intensity="light")

    # --- Парсинг ---
    if domain == "seloger":
        data = parse_seloger(page, html)
    elif domain == "bellesdemeures":
        data = parse_bellesdemeures(page, html)
    elif domain == "selogerneuf":
        data = parse_selogerneuf(page, html)
    else:
        data = {}

    if not any([data.get("title"), data.get("description"), data.get("surface")]):
        print(f"   [EMPTY]      ничего не спарсилось")
        return "Failed"

    for field, maxlen in [("title", 500), ("surface", 100), ("type", 100),
                          ("rooms", 50), ("price", 100), ("price_m2", 100),
                          ("location", 255)]:
        if data.get(field):
            setattr(item, field, data[field][:maxlen])
    if data.get("description"):
        item.description = data["description"][:10000]

    print(f"   [OK]         {time.monotonic() - t:.2f}s")
    return "Done"


# ===========================================================================
# MAIN
# ===========================================================================

def main():
    t0 = time.monotonic()

    chrome_path = find_chrome()
    if not chrome_path:
        print("[ERR] Chrome не найден")
        return

    existing = cdp_version()
    if existing:
        print(f"[CHROME] используем существующий CDP: {existing}")
    else:
        print("[CHROME] CDP не отвечает — запускаю")
        kill_our_chrome()
        launch_chrome(chrome_path)
        if not wait_for_cdp():
            print("[ERR] CDP не поднялся")
            return

    items = list(Product.objects.filter(status__in=["New", "Failed"]).order_by("id"))
    total = len(items)
    print(f"\n[MAIN] К обработке: {total}")
    if total == 0:
        return

    stats = {"done": 0, "failed": 0, "captcha": 0, "skipped": 0}

    with sync_playwright() as p:
        # Если CDP уже живой — просто подключаемся, не перезапускаем Chrome
        if existing:
            browser = p.chromium.connect_over_cdp(CDP_URL)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.new_page()
            print("[CHROME] подключились к существующему CDP")
        else:
            browser = p.chromium.connect_over_cdp(CDP_URL)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.new_page()

        try:
            for i, item in enumerate(items, 1):
                print(f"\n[{i}/{total}]")
                try:
                    status = parse_product(page, item)
                except Exception as e:
                    msg = str(e).lower()
                    if ("connection closed" in msg or "target closed" in msg
                            or "browser has been closed" in msg):
                        print(f"   [FATAL] Chrome умер: {e}")
                        try:
                            browser.close()
                        except Exception:
                            pass
                        try:
                            kill_our_chrome()
                            launch_chrome(chrome_path)
                            wait_for_cdp()
                            browser = p.chromium.connect_over_cdp(CDP_URL)
                            context = browser.contexts[0] if browser.contexts else browser.new_context()
                            page = context.new_page()
                        except Exception as e2:
                            print(f"   [FATAL] Не удалось перезапустить Chrome: {e2}")
                            return
                        status = "Failed"
                    else:
                        print(f"   [FATAL] {type(e).__name__}: {e}")
                        traceback.print_exc()
                        status = "Failed"

                if status is None:
                    print(f"[PROGRESS] {i}/{total} | (пропущено)")
                    stats["skipped"] += 1
                    if i < total:
                        time.sleep(random.uniform(*DELAY_BETWEEN_ITEMS))
                    continue

                try:
                    item.status = status
                    item.save()
                except Exception as e:
                    print(f"   [SAVE-ERR] {type(e).__name__}: {e}")

                if status == "Done":
                    stats["done"] += 1
                elif status == "Captcha":
                    stats["captcha"] += 1
                else:
                    stats["failed"] += 1

                print(f"[PROGRESS] {i}/{total} | ok={stats['done']} "
                      f"fail={stats['failed']} captcha={stats['captcha']} "
                      f"skipped={stats['skipped']}")

                if i < total:
                    d = random.uniform(*DELAY_BETWEEN_ITEMS)
                    print(f"[PAUSE] {d:.1f}s")
                    time.sleep(d)
        finally:
            try:
                browser.close()
            except Exception:
                pass

    print("\n" + "=" * 60)
    print(f"[SUMMARY] Всего:   {total}")
    print(f"          OK:      {stats['done']}")
    print(f"          Failed:  {stats['failed']}")
    print(f"          Captcha: {stats['captcha']}")
    print(f"          Skipped: {stats['skipped']}")
    print(f"          Время:   {time.monotonic() - t0:.1f}s")
    print("=" * 60)


if __name__ == "__main__":
    main()