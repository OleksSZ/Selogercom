"""
Парсер объявлений SeLoger — аренда (Rent) квартир в Иль-де-Франс.

Особенности:
- Дедупликация через set + get_or_create.
- Статус Done ставится ТОЛЬКО если все страницы прошли успешно.
- Флаги CLEAR_PRODUCTS и RESET_LOCATIONS управляют очисткой.
- Fallback на два набора селекторов (старый/новый SERP).
"""

import json
import re

from load_django import *
from parser_app.models import Product, Location

import requests
from bs4 import BeautifulSoup


# ===========================================================================
# ФЛАГИ УПРАВЛЕНИЯ
# ===========================================================================
# Поставь True, чтобы выполнить действие ОДИН РАЗ, потом верни False.
CLEAR_PRODUCTS = True         # удалить ВСЕ записи из Product перед парсингом
RESET_LOCATIONS = False       # сбросить все Location в статус "New"
DELETE_DONE_LOCATIONS = False # удалить Location со статусом "Done"

# Сколько страниц парсить
PAGES_TO_PARSE = range(1, 5)


# ===========================================================================
# НАСТРОЙКИ
# ===========================================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/138.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Referer": "https://www.seloger.com/",
    "Cache-Control": "max-age=0",
    "Upgrade-Insecure-Requests": "1",
    "DNT": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
}

# Новый URL: аренда (Rent), квартиры (Apartment), Иль-де-Франс (AD04FR5)
BASE_URL = (
    "https://www.seloger.com/classified-search"
    "?distributionTypes=Rent"
    "&estateTypes=Apartment"
    "&locations=AD04FR5"
    "&method=form"
    "&page={page}"
)

REQUEST_TIMEOUT = 20

PRICE_MAX_LEN = 100
LOCATION_MAX_LEN = 255


# ===========================================================================
# УТИЛИТЫ
# ===========================================================================

def clean(text):
    if not text:
        return None
    return text.replace("\u202f", " ").replace("\xa0", " ").strip()


def safe_truncate(value, max_len):
    if value is None:
        return None
    return str(value)[:max_len]


def normalize_url(href):
    """Приводит относительные ссылки к абсолютным."""
    if not href:
        return None
    if href.startswith("/"):
        return "https://www.seloger.com" + href
    return href


# ===========================================================================
# УПРАВЛЕНИЕ БД
# ===========================================================================

def clear_products():
    """Удаляет все Product."""
    count = Product.objects.count()
    Product.objects.all().delete()
    print(f"[CLEAR] Удалено Product: {count}")


def reset_locations():
    """Сбрасывает все Location в статус New."""
    updated = Location.objects.all().update(status="New")
    print(f"[RESET] Location → New: {updated}")


def delete_done_locations():
    """Удаляет Location со статусом Done."""
    qs = Location.objects.filter(status="Done")
    count = qs.count()
    qs.delete()
    print(f"[CLEAR] Удалено Done-Location: {count}")


def ensure_locations():
    """Гарантирует, что есть хотя бы один Location со статусом New."""
    total = Location.objects.count()
    print(f"[DB] Всего Location: {total}")
    print(f"[DB] Статусы: {list(Location.objects.values_list('status', flat=True).distinct())}")

    if total == 0:
        Location.objects.create(url=BASE_URL, status="New")
        print("[DB] Создано 1 Location.")

    new_count = Location.objects.filter(status="New").count()
    if new_count == 0:
        Location.objects.update(status="New")
        print(f"[DB] Сброшено на New: {Location.objects.filter(status='New').count()}")

    for loc in Location.objects.filter(status="New"):
        if "{page}" not in (loc.url or ""):
            loc.url = BASE_URL
            loc.save()
            print(f"[DB] Location id={loc.id} → URL заменён на BASE_URL")


# ===========================================================================
# ПАРСИНГ
# ===========================================================================

def parse_cards_from_html(soup):
    """
    Карточки из HTML. Поддерживает два набора data-testid:
    - новый: sl.explore.card-container
    - старый: serp-core-classified-card-testid
    """
    results = []

    cards = soup.find_all("div", attrs={"data-testid": "sl.explore.card-container"})
    if not cards:
        cards = soup.find_all("div", attrs={"data-testid": "serp-core-classified-card-testid"})

    print(f"   [HTML] Карточек найдено: {len(cards)}")

    for card in cards:
        product = {"link": None, "price": None, "price_m2": None, "location": None}

        # --- Ссылка ---
        a_tag = card.find("a", href=True)
        if a_tag:
            product["link"] = clean(normalize_url(a_tag["href"]))

        # --- Цена ---
        price_el = (
            card.find(attrs={"data-testid": "sl.explore-card-price"})
            or card.find("div", attrs={"data-testid": "cardmfe-price-testid"})
        )
        if price_el:
            # В старом формате цена = первый прямой текстовый child
            first_text = None
            for child in price_el.children:
                if isinstance(child, str) and child.strip():
                    first_text = child
                    break
            if first_text:
                product["price"] = clean(first_text)
            else:
                product["price"] = clean(price_el.get_text(strip=True))

            # Цена за м² — в <span>
            span = price_el.find("span")
            if span:
                product["price_m2"] = clean(span.get_text(strip=True))

        # --- Локация ---
        loc_el = (
            card.find(attrs={"data-testid": "sl.explore-card-location"})
            or card.find("div", attrs={"data-testid": "cardmfe-description-box-address"})
        )
        if loc_el:
            product["location"] = clean(loc_el.get_text(strip=True))

        # Пропускаем карточки без ссылки
        if product["link"]:
            results.append(product)

    return results


def parse_from_fetcher_json(soup):
    """
    Объявления из __UFRN_FETCHER__ (или его новых вариантов).
    Пробуем несколько id скриптов, чтобы пережить обновление фронта.
    """
    script = (
        soup.find("script", id="__UFRN_FETCHER__")
        or soup.find("script", id="__UFRN_LIFECYCLE_SERVERREQUEST__")
        or soup.find("script", id="__UFRN_STORE__")
    )
    if not script or not script.string:
        print("   [JSON] служебный JSON-скрипт не найден")
        return []

    try:
        match = re.search(r"JSON\.parse\((.*?)\);", script.string, re.S)
        if not match:
            print("   [JSON] Не найден JSON.parse(...)")
            return []

        # Аккуратное декодирование (без порчи UTF-8)
        inner = json.loads(match.group(1))   # строка после JSON.parse
        data = json.loads(inner)              # объект

        # Пробуем разные места, где может лежать список объявлений
        ads = (
            data.get("data", {})
            .get("classified-serp-init-data", {})
            .get("pageProps", {})
            .get("classifiedsData", {})
        )
        if not ads:
            # fallback: ищем в новом формате
            ads = (
                data.get("data", {})
                .get("classifiedsData", {})
            ) or {}

        print(f"   [JSON] Объявлений в JSON: {len(ads)}")

        results = []
        for ad_id, ad in ads.items():
            hard = ad.get("hardFacts", {}) or {}
            addr = (ad.get("location", {}) or {}).get("address", {}) or {}
            price = (hard.get("price", {}) or {}).get("value")

            price_m2 = None
            for fact in hard.get("facts", []) or []:
                if fact.get("type") == "pricePerSquareMeter":
                    price_m2 = fact.get("value")
                    break

            location = ", ".join(
                filter(None, [
                    addr.get("district"),
                    addr.get("city"),
                    f"({addr.get('zipCode')})" if addr.get("zipCode") else None,
                ])
            )

            link = clean(ad.get("url"))
            if link and link.startswith("/"):
                link = "https://www.seloger.com" + link

            results.append({
                "link": link,
                "price": clean(price),
                "price_m2": clean(price_m2),
                "location": clean(location),
            })

        return results

    except Exception as e:
        print(f"   [JSON] Ошибка парсинга: {type(e).__name__}: {e}")
        return []


# ===========================================================================
# СОХРАНЕНИЕ
# ===========================================================================

def save_products(products, seen_links):
    """Сохраняет через get_or_create, дедуп в памяти через seen_links."""
    saved = 0
    skipped = 0

    for p in products:
        link = p.get("link")
        if not link:
            continue
        if link in seen_links:
            continue
        seen_links.add(link)

        price = safe_truncate(p.get("price"), PRICE_MAX_LEN)
        price_m2 = safe_truncate(p.get("price_m2"), PRICE_MAX_LEN)
        location = safe_truncate(p.get("location"), LOCATION_MAX_LEN)

        obj, created = Product.objects.get_or_create(
            link=link,
            defaults={
                "price": price,
                "price_m2": price_m2,
                "location": location,
            },
        )
        if created:
            saved += 1
            print(f"   ✅ Saved: {link} | {price} | {price_m2} | {location}")
        else:
            skipped += 1

    print(f"   [SAVE] Новых: {saved}, уже было: {skipped}")
    return saved


# ===========================================================================
# ОСНОВНОЙ ЦИКЛ
# ===========================================================================

def main():
    # 1. Очистки по флагам
    if CLEAR_PRODUCTS:
        clear_products()
    if DELETE_DONE_LOCATIONS:
        delete_done_locations()
    if RESET_LOCATIONS:
        reset_locations()

    # 2. Гарантируем наличие Location
    ensure_locations()

    # 3. Основной цикл
    items = Location.objects.filter(status="New").order_by("id")
    print(f"\n[MAIN] К обработке Location: {items.count()}")

    for item in items:
        print(f"\n[MAIN] Location id={item.id} | {item.url}")
        seen_links = set()

        pages_ok = 0
        pages_failed = 0
        total_saved = 0

        for page in PAGES_TO_PARSE:
            try:
                url = item.url.format(page=page)
            except KeyError as e:
                print(f"   ❌ Ошибка формата URL: {e}")
                pages_failed += 1
                continue

            print(f"\n   --- Страница {page}: {url}")

            # --- HTTP-запрос ---
            try:
                r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            except Exception as e:
                print(f"   ❌ Request error: {type(e).__name__}: {e}")
                pages_failed += 1
                continue

            print(f"   [HTTP] Status: {r.status_code}, len={len(r.text)}")

            if r.status_code != 200:
                print(f"   ⚠️  Не 200. Первые 200 символов:")
                print(r.text[:200])
                pages_failed += 1
                continue

            if "captcha" in r.text.lower() or "datadome" in r.text.lower():
                print("   🛑 Похоже на капчу/блок DataDome")
                pages_failed += 1
                continue

            # --- Парсинг ---
            soup = BeautifulSoup(r.text, "html.parser")

            try:
                total_saved += save_products(parse_cards_from_html(soup), seen_links)
                total_saved += save_products(parse_from_fetcher_json(soup), seen_links)
                pages_ok += 1
            except Exception as e:
                print(f"   ❌ Ошибка парсинга страницы {page}: {type(e).__name__}: {e}")
                pages_failed += 1
                continue

        # --- Статус ---
        if pages_ok == 0:
            print(f"\n[MAIN] Location id={item.id} → ❌ Все страницы упали. Оставляю New.")
        elif pages_failed > 0:
            print(f"\n[MAIN] Location id={item.id} → ⚠️  Частично "
                  f"(ok={pages_ok}, fail={pages_failed}). Оставляю New.")
        else:
            item.status = "Done"
            item.save()
            print(f"\n[MAIN] Location id={item.id} → ✅ Done "
                  f"(новых сохранено: {total_saved})")


if __name__ == "__main__":
    main()