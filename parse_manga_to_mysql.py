import time
import logging
import re
import json
import os
import unicodedata
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit, parse_qsl, urlencode

import requests
from bs4 import BeautifulSoup
import mysql.connector
from mysql.connector import Error

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

DB_CONFIG = {
    "host": "localhost",
    "user": "root",
    "password": "root",
    "database": "manga_db",
    "charset": "utf8mb4",
}


# ---------------------------------------------------------------------------
# 1. Работа с базой данных
# ---------------------------------------------------------------------------

def get_connection():
    return mysql.connector.connect(**DB_CONFIG)


def init_db():
    """Создаёт основную и связанные таблицы, сохраняя совместимость со старой схемой."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS manga (
            id INT AUTO_INCREMENT PRIMARY KEY,
            title VARCHAR(500) NOT NULL,
            cover_url VARCHAR(1000),
            author VARCHAR(255),
            genres TEXT,
            status VARCHAR(100),
            description TEXT,
            year VARCHAR(10),
            rate VARCHAR(10),
            UNIQUE KEY uniq_title (title)
        ) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci
        """
    )
    # Если таблица была создана старой версией с genres VARCHAR(500),
    # автоматически переводим поле в TEXT. Это устраняет ошибку 1406.
    try:
        cur.execute("ALTER TABLE manga MODIFY COLUMN genres TEXT NULL")
    except Error as e:
        log.warning("Не удалось обновить поле genres: %s", e)

    # Новые поля: ромадзи/японское название, локальный путь к обложке
    # и количество найденных глав. Персонажи хранятся отдельно.
    # ADD COLUMN оборачиваем в try/except, т.к. старые MySQL (<8.0.29)
    # не поддерживают "ADD COLUMN IF NOT EXISTS" — ошибка 1060
    # (Duplicate column name) означает, что колонка уже есть, это нормально.
    new_columns = {
        "romaji": "VARCHAR(500) NULL",
        "japanese_name": "VARCHAR(500) NULL",
        "cover_path": "VARCHAR(500) NULL",
        "chapters_count": "INT NULL",
    }
    for col, definition in new_columns.items():
        try:
            cur.execute(f"ALTER TABLE manga ADD COLUMN {col} {definition}")
        except Error as e:
            if e.errno != 1060:
                log.warning("Не удалось добавить колонку %s: %s", col, e)

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS chapters (
            id INT AUTO_INCREMENT PRIMARY KEY,
            manga_id INT NOT NULL,
            title VARCHAR(500) NOT NULL,
            chapter_number DECIMAL(10, 3) NULL,
            source_url VARCHAR(700) NOT NULL,
            chapter_index INT NOT NULL,
            UNIQUE KEY uniq_manga_chapter_url (manga_id, source_url),
            CONSTRAINT fk_chapters_manga FOREIGN KEY (manga_id)
                REFERENCES manga(id) ON DELETE CASCADE
        ) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS pages (
            id INT AUTO_INCREMENT PRIMARY KEY,
            chapter_id INT NOT NULL,
            page_number INT NOT NULL,
            image_url VARCHAR(1000) NOT NULL,
            file_path VARCHAR(1000) NOT NULL,
            UNIQUE KEY uniq_chapter_page (chapter_id, page_number),
            CONSTRAINT fk_pages_chapter FOREIGN KEY (chapter_id)
                REFERENCES chapters(id) ON DELETE CASCADE
        ) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS characters (
            id INT AUTO_INCREMENT PRIMARY KEY,
            manga_id INT NOT NULL,
            name VARCHAR(500) NOT NULL,
            image_url VARCHAR(1000) NULL,
            image_path VARCHAR(1000) NULL,
            UNIQUE KEY uniq_manga_character (manga_id, name),
            CONSTRAINT fk_characters_manga FOREIGN KEY (manga_id)
                REFERENCES manga(id) ON DELETE CASCADE
        ) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci
        """
    )

    conn.commit()
    cur.close()
    conn.close()


def save_manga(item: dict) -> int | None:
    """Сохраняет мангу и возвращает её id для связанных таблиц."""
    conn = get_connection()
    cur = conn.cursor()
    sql = """
        INSERT INTO manga (
            title, cover_url, cover_path, author, genres, status, description,
            year, rate, romaji, japanese_name, chapters_count
        )
        VALUES (
            %(title)s, %(cover_url)s, %(cover_path)s, %(author)s, %(genres)s,
            %(status)s, %(description)s, %(year)s, %(rate)s, %(romaji)s,
            %(japanese_name)s, %(chapters_count)s
        )
        ON DUPLICATE KEY UPDATE
            cover_url = VALUES(cover_url),
            cover_path = VALUES(cover_path),
            author = VALUES(author),
            genres = VALUES(genres),
            status = VALUES(status),
            description = VALUES(description),
            year = VALUES(year),
            rate = VALUES(rate),
            romaji = VALUES(romaji),
            japanese_name = VALUES(japanese_name),
            chapters_count = COALESCE(VALUES(chapters_count), chapters_count)
    """
    try:
        # Защита от слишком длинных значений даже в старой/неожиданной схеме.
        limits = {
            "title": 500, "cover_url": 1000, "cover_path": 500,
            "author": 255, "status": 100, "year": 10, "rate": 10,
            "romaji": 500, "japanese_name": 500,
        }
        for key, limit in limits.items():
            if isinstance(item.get(key), str):
                item[key] = item[key][:limit]

        params = dict(item)
        params.pop("characters", None)
        # Гарантируем, что в SQL уйдут все нужные ключи, даже если их не было в item.
        for key in ("cover_path", "romaji", "japanese_name", "rate", "year", "chapters_count"):
            params.setdefault(key, None)

        cur.execute(sql, params)
        conn.commit()
        cur.execute("SELECT id FROM manga WHERE title = %s", (item.get("title"),))
        manga_row = cur.fetchone()
        log.info("БД: сохранена манга '%s'", item.get("title"))
        return manga_row[0] if manga_row else None
    except Error as e:
        conn.rollback()
        log.error("Ошибка записи в БД: %s", e)
        return None
    finally:
        cur.close()
        conn.close()


def save_characters(manga_id: int | None, characters: list[dict]) -> None:
    """Сохраняет персонажей отдельно и связывает их с мангой."""
    if not manga_id:
        return
    conn = get_connection()
    cur = conn.cursor()
    try:
        for character in characters:
            cur.execute(
                """
                INSERT INTO characters (manga_id, name, image_url, image_path)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    image_url = VALUES(image_url), image_path = VALUES(image_path)
                """,
                (manga_id, character.get("name"), character.get("image_url"),
                 character.get("image")),
            )
        conn.commit()
    except Error as e:
        conn.rollback()
        log.error("Ошибка записи персонажей: %s", e)
    finally:
        cur.close()
        conn.close()


def save_chapter_pages(manga_id: int | None, chapter: dict, pages: list[dict]) -> None:
    """Сохраняет главу и её страницы с внешними ключами."""
    if not manga_id:
        return
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO chapters (manga_id, title, chapter_number, source_url, chapter_index)
            VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                title = VALUES(title), chapter_number = VALUES(chapter_number),
                chapter_index = VALUES(chapter_index)
            """,
            (manga_id, chapter["title"], chapter.get("number"), chapter["url"],
             chapter["index"]),
        )
        cur.execute(
            "SELECT id FROM chapters WHERE manga_id = %s AND source_url = %s",
            (manga_id, chapter["url"]),
        )
        chapter_id = cur.fetchone()[0]
        for page in pages:
            cur.execute(
                """
                INSERT INTO pages (chapter_id, page_number, image_url, file_path)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE image_url = VALUES(image_url), file_path = VALUES(file_path)
                """,
                (chapter_id, page["number"], page["url"], page["file_path"]),
            )
        conn.commit()
    except Error as e:
        conn.rollback()
        log.error("Ошибка записи главы и страниц: %s", e)
    finally:
        cur.close()
        conn.close()


# ---------------------------------------------------------------------------
# 2. Загрузка страницы
# ---------------------------------------------------------------------------

HTTP_SESSION = requests.Session()
HTTP_SESSION.headers.update(HEADERS)


def fetch(url: str) -> BeautifulSoup | None:
    """Загружает HTML через общую сессию, чтобы сохранять cookies сайта."""
    try:
        resp = HTTP_SESSION.get(url, timeout=15)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding
        return BeautifulSoup(resp.text, "lxml")
    except requests.RequestException as e:
        log.error("Не удалось загрузить %s: %s", url, e)
        return None



def make_empty_item(source: str, url: str) -> dict:
    """Создаёт нормализованную запись манги из прямого URL.

    В режиме urls.txt поиск по названию и sitemap не используется.
    """
    return {
        "source": source,
        "url": url,
        "title": None,
        "cover_url": None,
        "cover_path": None,
        "author": None,
        "genres": None,
        "status": None,
        "description": None,
        "romaji": None,
        "japanese_name": None,
        "rate": None,
        "year": None,
        "chapters_count": None,
        "characters": [],
    }


# ---------------------------------------------------------------------------
# 3. Общий парсер для движка Grouple (seimanga, zazaza/readmanga, mintmanga
#    и другие сайты этой сети имеют одинаковую вёрстку страницы тайтла)
# ---------------------------------------------------------------------------

# Диапазоны юникода для японской азбуки (хирагана/катакана) и кандзи —
# используем, чтобы отличить японское название от ромадзи/английского
# среди альтернативных названий.
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]")

MAX_CHARACTERS = 6


def parse_grouple_detail(item: dict):
    """
    Заходим на страницу конкретной манги и заполняем поля.
    Селекторы основаны на реальной структуре страницы Grouple-движка
    (проверено на странице seimanga.me/spokoinoi_nochi__punpun).
    Если у конкретного сайта верстка немного отличается — часть полей
    может не найтись, тогда просто останется None, остальное соберётся.

    cover_url/characters[].image_url тут — ещё ССЫЛКИ, а не локальные файлы:
    сами картинки скачивает вызывающий код (после того как известно
    итоговое название манги — оно нужно для имени папки), через
    download_cover() / download_characters().
    """
    soup = fetch(item["url"])
    if not soup:
        return item

    # --- Название (русское и английское) ---
    name_tag = soup.select_one("h1.cr-hero-names__main, h1.names .name, h1 .name")
    eng_name_tag = soup.select_one(".cr-hero-names__eng, h1.names .eng-name, .eng-name")

    if name_tag:
        item["title"] = name_tag.get_text(strip=True)
    elif eng_name_tag:
        item["title"] = eng_name_tag.get_text(strip=True)

    # --- Обложка (ссылка, скачивание — отдельным шагом) ---
    cover_tag = soup.select_one("img.cr-hero-poster__img, .subject-cower img, .picture-fotorama img")
    if cover_tag:
        item["cover_url"] = cover_tag.get("data-src") or cover_tag.get("src")

    # --- Автор ---
    author_tag = soup.select_one(".cr-main-person-item__name a")
    item["author"] = author_tag.get_text(strip=True) if author_tag else None

    # --- Жанры (несколько ссылок) ---
    genre_tags = soup.select(".creation-element-tags__item span")
    item["genres"] = ", ".join(g.get_text(strip=True) for g in genre_tags) or None

    # --- Статус (может быть несколько таких блоков: выпуск и перевод) ---
    status_el = soup.select_one(".cr-info-details-item__status")
    item["status"] = status_el.get_text(strip=True) if status_el else None

    # --- Описание ---
    descr_tag = soup.select_one(".cr-description__content")
    item["description"] = descr_tag.get_text(strip=True) if descr_tag else None

    # --- Ромадзи и японское название ---
    # Ищем по уникальному тексту тултипа, а не по классу-обёртке (он неизвестен
    # из присланного фрагмента), внутри — несколько <span>, разделённых
    # <span class="cr-hero-names__alt-separator">/</span>. Японское название
    # определяем по наличию иероглифов/каны, остальное считаем ромадзи.
    alt_names_tag = soup.find("span", attrs={"data-tippy-content": re.compile("показать весь список", re.I)})
    romaji_parts, japanese_parts = [], []
    if alt_names_tag:
        for span in alt_names_tag.find_all("span", recursive=False):
            if "cr-hero-names__alt-separator" in (span.get("class") or []):
                continue
            text = span.get_text(strip=True)
            if not text:
                continue
            (japanese_parts if _CJK_RE.search(text) else romaji_parts).append(text)
    item["romaji"] = " / ".join(romaji_parts) or None
    item["japanese_name"] = " / ".join(japanese_parts) or None

    # --- Рейтинг ---
    rating_tag = soup.select_one(".cr-hero-rating__value")
    item["rate"] = rating_tag.get_text(strip=True) if rating_tag else None

    # --- Год ---
    year_tag = soup.select_one("a.cr-hero-short-details__item[href*='/list/year/']")
    item["year"] = None
    if year_tag:
        m = re.search(r"(\d{4})", year_tag.get_text())
        if m:
            item["year"] = m.group(1)

    # --- Персонажи (первые MAX_CHARACTERS штук, если есть) ---
    item["characters"] = parse_characters(soup, item["url"])

    return item


def parse_characters(soup: BeautifulSoup, manga_url: str, limit: int = MAX_CHARACTERS) -> list[dict]:
    """Достаёт первых `limit` персонажей манги: имя + ссылка на картинку.

    Картинки тут ещё не скачаны (это делает download_characters() позже,
    когда известно итоговое название манги для имени папки).
    """
    characters = []
    seen_names = set()
    for a in soup.select("a[href*='/list/person/']"):
        if len(characters) >= limit:
            break
        title_tag = a.select_one(".entity-card-tile__title")
        name = title_tag.get_text(strip=True) if title_tag else None
        if not name or name in seen_names:
            continue
        img_tag = a.select_one("img.ui-cover")
        image_url = None
        if img_tag:
            raw = img_tag.get("data-src") or img_tag.get("src")
            if raw:
                image_url = _clean_url(raw, manga_url)
        seen_names.add(name)
        characters.append({"name": name, "image_url": image_url})
    return characters



# ---------------------------------------------------------------------------
# 4. Парсер глав и страниц главы
# ---------------------------------------------------------------------------

CHAPTERS_DIR = Path("chapters")
COVERS_DIR = Path("covers")
CHARACTERS_DIR = Path("characters")
DOWNLOAD_TIMEOUT = 30
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")


def _clean_url(url: str, base_url: str) -> str:
    return urljoin(base_url, url.strip())


def _chapter_number(text: str, url: str = "") -> float:
    """Извлекает номер главы для сортировки. Например 12.5 -> 12.5."""
    value = f"{text} {url}".lower().replace(",", ".")
    patterns = [
        r"(?:глава|chapter|chap|том|тома|volume)\s*[-№#:]?\s*(\d+(?:\.\d+)?)",
        r"/(?:chapter|chap|ch)[-_]?(\d+(?:\.\d+)?)(?:[/_.-]|$)",
        r"(?:^|[\s_-])(\d+(?:\.\d+)?)(?:\s*$|[/_.-])",
    ]
    for pattern in patterns:
        m = re.search(pattern, value, re.I)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
    return float("inf")


def _chapter_key(text: str, url: str) -> str:
    n = _chapter_number(text, url)
    if n != float("inf"):
        return f"{n:g}"
    return _safe_name(text)


_TRANSLITERATION = str.maketrans({
    "А": "A", "Б": "B", "В": "V", "Г": "G", "Д": "D", "Е": "E",
    "Ё": "E", "Ж": "Zh", "З": "Z", "И": "I", "Й": "Y", "К": "K",
    "Л": "L", "М": "M", "Н": "N", "О": "O", "П": "P", "Р": "R",
    "С": "S", "Т": "T", "У": "U", "Ф": "F", "Х": "Kh", "Ц": "Ts",
    "Ч": "Ch", "Ш": "Sh", "Щ": "Sch", "Ъ": "", "Ы": "Y", "Ь": "",
    "Э": "E", "Ю": "Yu", "Я": "Ya",
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e",
    "ё": "e", "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k",
    "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya",
    "І": "I", "і": "i", "Ї": "Yi", "ї": "yi", "Є": "Ye", "є": "ye",
    "Ґ": "G", "ґ": "g",
})


def _safe_name(text: str) -> str:
    """Возвращает безопасное имя пути только из латиницы, цифр и `_`/`.`/`-`."""
    transliterated = (text or "").translate(_TRANSLITERATION)
    transliterated = unicodedata.normalize("NFKD", transliterated)
    transliterated = transliterated.encode("ascii", "ignore").decode("ascii")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", transliterated).strip("._")
    return safe or "unknown"


def parse_chapter_links(manga_url: str) -> list[dict]:
    """Находит именно ссылки из списка глав, а не любые числовые ссылки страницы."""
    soup = fetch(manga_url)
    if not soup:
        return []

    found = {}
    # Сначала ищем контейнер, в котором находится заголовок «Читать главы».
    heading = None
    for tag in soup.find_all(re.compile(r"^h[1-6]$")):
        text = tag.get_text(" ", strip=True).lower()
        if "читать главы" in text or "список глав" in text:
            heading = tag
            break

    containers = []
    if heading:
        parent = heading.parent
        for _ in range(4):
            if parent:
                containers.append(parent)
                parent = parent.parent
    # Дополнительные известные контейнеры Grouple.
    for selector in (".manga-chapters", ".chapters", ".chapter-list", ".table-list", ".subject-chapters"):
        containers.extend(soup.select(selector))

    candidates = []
    for container in containers:
        candidates.extend(container.select("a[href]"))

    # Если контейнер не определился, берём ссылки, URL которых имеют структуру /volN/N.
    if not candidates:
        candidates = [a for a in soup.select("a[href]")
                      if re.search(r"/vol\d+(?:[./_-]|/|$)", a.get("href", ""), re.I)]

    for a in candidates:
        href = a.get("href")
        if not href:
            continue
        url = _clean_url(href, manga_url)
        parsed = urlparse(url)
        path = parsed.path.rstrip("/")
        # Для Grouple-сайтов глава имеет /vol<том>/<глава>.
        m = re.search(r"/vol(\d+(?:[.\-]\d+)?)/(\d+(?:[.\-]\d+)?)$", path, re.I)
        if not m:
            continue
        text = " ".join(a.stripped_strings).strip()
        # Текст вида «1 - 1 Нана Комацу» — сохраняем целиком.
        volume = m.group(1).replace("-", ".")
        chapter_no = m.group(2).replace("-", ".")
        num = float(chapter_no)
        key = url.split("#", 1)[0]
        found[key] = {
            "url": url,
            "title": text or f"{volume} - {chapter_no}",
            "volume": volume,
            "chapter_number": num,
        }

    chapters = list(found.values())
    chapters.sort(key=lambda x: (x["chapter_number"], float(x["volume"]), x["url"]))
    return chapters


def _make_chrome_driver():
    """Создаёт webdriver.Chrome с возможностью ручной настройки путей.

    Если Selenium Manager не может сам скачать/найти chromedriver или Chrome
    (частая ситуация в РФ — сервисы Google для автозагрузки chromedriver
    могут быть недоступны/медленные, из-за чего падает с 'Service Unavailable'),
    можно один раз скачать нужные файлы руками и указать пути через
    переменные окружения, не трогая код:

        CHROMEDRIVER_PATH=C:\\tools\\chromedriver.exe
        CHROME_BINARY_PATH=C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe

    (в PowerShell: $env:CHROMEDRIVER_PATH="C:\\tools\\chromedriver.exe")

    ВАЖНО про 'Service Unavailable': если в системе настроен HTTP(S)-прокси
    (переменные HTTP_PROXY/HTTPS_PROXY/ALL_PROXY или системный прокси Windows),
    Python-клиент Selenium заворачивает через него ДАЖЕ ЛОКАЛЬНОЕ соединение
    Python -> chromedriver (127.0.0.1:порт). Прокси не умеет его обработать
    и отвечает заглушкой 'Service Unavailable' вместо ответа chromedriver.
    Поэтому ниже принудительно исключаем localhost/127.0.0.1 из прокси —
    это не влияет на то, как сам браузер Chrome выходит в интернет
    (у него собственные сетевые настройки), только на служебный канал
    Python <-> chromedriver.
    """
    for var in ("NO_PROXY", "no_proxy"):
        existing = os.environ.get(var, "")
        parts = [p for p in existing.split(",") if p.strip()]
        for host in ("127.0.0.1", "localhost"):
            if host not in parts:
                parts.append(host)
        os.environ[var] = ",".join(parts)

    options = ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1600,1200")
    options.add_argument("--lang=ru-RU")

    chrome_binary = os.environ.get("CHROME_BINARY_PATH")
    if chrome_binary:
        options.binary_location = chrome_binary

    chromedriver_path = os.environ.get("CHROMEDRIVER_PATH")
    if chromedriver_path:
        from selenium.webdriver.chrome.service import Service as ChromeService
        service = ChromeService(executable_path=chromedriver_path)
        return webdriver.Chrome(service=service, options=options)
    return webdriver.Chrome(options=options)


def _current_manga_img_src(driver, chapter_url: str) -> str | None:
    """Достаёт URL текущей отображаемой страницы из img.manga-img."""
    src = driver.execute_script(
        """
        var img = document.querySelector('img.manga-img');
        if (!img) return null;
        return img.currentSrc || img.getAttribute('src') ||
               img.getAttribute('data-src') || img.getAttribute('data-original') || null;
        """
    )
    if not src or src.startswith("data:image/"):
        return None
    return _clean_url(src, chapter_url)


def _click_next_page(driver):
    """Кликает по правой части изображения — так в этом ридере пролистывается
    следующая страница (левая часть обычно листает назад)."""
    driver.execute_script(
        """
        var img = document.querySelector('img.manga-img');
        if (!img) return false;
        var rect = img.getBoundingClientRect();
        var x = rect.left + rect.width * 0.85;
        var y = rect.top + rect.height * 0.5;
        var el = document.elementFromPoint(x, y) || img;
        ['mousedown', 'mouseup', 'click'].forEach(function (type) {
            el.dispatchEvent(new MouseEvent(type, {
                bubbles: true, cancelable: true, clientX: x, clientY: y
            }));
        });
        return true;
        """
    )


def _selenium_chapter_images(chapter_url: str) -> list[str]:
    """Постранично пролистывает главу и собирает все img.manga-img.

    Это не бесконечный скролл — ридер показывает ОДНУ страницу за раз,
    и клик по правой части картинки переключает на следующую (клиентский JS
    подменяет содержимое img.manga-img). Поэтому вместо скролла эмулируем
    клик по правой части изображения и после каждого клика ждём, пока
    src у img.manga-img изменится, пока не наберём предполагаемое число
    страниц/пока src не перестанет меняться (конец главы) — с защитой от
    случайного перехода на следующую главу и от зацикливания.
    """
    if not SELENIUM_AVAILABLE:
        return []
    driver = None
    try:
        driver = _make_chrome_driver()
        chapter_path = urlparse(chapter_url).path.rstrip("/")
        driver.get(chapter_url)
        WebDriverWait(driver, 30).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        WebDriverWait(driver, 20).until(
            lambda d: d.execute_script("return document.querySelectorAll('img.manga-img').length") > 0
        )
        time.sleep(0.3)

        result = []
        seen = set()
        stagnant = 0
        MAX_PAGES = 500  # защитный предел, чтобы не зациклиться

        for _ in range(MAX_PAGES):
            src = _current_manga_img_src(driver, chapter_url)

            if src and src not in seen:
                seen.add(src)
                result.append(src)
                stagnant = 0
            else:
                stagnant += 1

            # Ушли на другую главу (обычно клик по последней странице
            # уводит на следующую главу) — эти картинки сюда не относятся.
            new_path = urlparse(driver.current_url).path.rstrip("/")
            if new_path != chapter_path:
                if src and result and result[-1] == src:
                    # Эта картинка уже успела относиться к новой главе — убираем.
                    result.pop()
                break

            if stagnant >= 3:
                # Несколько кликов подряд без новой страницы — похоже, это конец главы.
                break

            _click_next_page(driver)
            try:
                WebDriverWait(driver, 5).until(
                    lambda d: _current_manga_img_src(d, chapter_url) != src
                    or urlparse(d.current_url).path.rstrip("/") != chapter_path
                )
            except Exception:
                pass  # клик мог не сработать - следующая итерация попробует снова

        log.info("Ридер (постраничный клик): найдено страниц: %s", len(result))
        for n, u in enumerate(result[:10], 1):
            log.info("  страница %03d: %s", n, u)
        if len(result) > 10:
            log.info("  ... ещё %s страниц", len(result) - 10)
        return result
    except Exception as e:
        # exc_info=True печатает полный traceback selenium, а не обрезанный
        # str(e) — по нему обычно видна настоящая причина (не найден chrome,
        # не скачался chromedriver, антивирус заблокировал процесс и т.д.).
        log.warning("Не удалось получить img.manga-img через браузер: %s", e, exc_info=True)
        log.warning(
            "Если ошибка похожа на 'Service Unavailable' или Selenium не может "
            "сам найти/скачать chromedriver: скачайте Chrome (обычную десктопную "
            "версию, не из Microsoft Store) и подходящий chromedriver.exe вручную, "
            "затем укажите пути через переменные окружения CHROME_BINARY_PATH и "
            "CHROMEDRIVER_PATH (см. docstring _make_chrome_driver)."
        )
        return []
    finally:
        if driver:
            driver.quit()

def _with_query_param(url: str, key: str, value: str) -> str:
    """Добавляет/заменяет query-параметр, сохраняя остальные параметры."""
    parts = urlsplit(url)
    params = parse_qsl(parts.query, keep_blank_values=True)
    params = [(k, v) for k, v in params if k.lower() != key.lower()]
    params.append((key, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), parts.fragment))


def _is_grouple_reader_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(part in host for part in ("zazaza", "seimanga", "readmanga", "mintmanga", "selfmanga"))


def _looks_like_age_gate(soup: BeautifulSoup | None) -> bool:
    """Распознаёт страницу-предупреждение 18+ без привязки к одному классу."""
    if not soup:
        return False
    text = soup.get_text(" ", strip=True).lower()
    markers = (
        "если вам больше 18",
        "вам больше 18 лет",
        "продолжить чтение",
        "возрастное ограничение",
        "манга для взрослых",
        "эта манга может содержать",
    )
    return any(marker in text for marker in markers)


def parse_chapter_images(chapter_url: str) -> tuple[list[str], str]:
    """Получает страницы ридера и возвращает (urls, effective_page_url).

    Для ZazaZa/SeiManga у тайтлов 18+ исходная страница главы может показывать
    только предупреждение. При нажатии «продолжить» сайт переводит на тот же
    URL с ``?mtr=true``. Поэтому после пустого HTML автоматически пробуем этот
    вариант в той же HTTP-сессии, сохраняя cookies.

    Ищем исключительно ``img.manga-img`` — никакие другие картинки страницы
    не рассматриваются.
    """
    candidate_urls = [chapter_url]
    mtr_url = _with_query_param(chapter_url, "mtr", "true")
    if _is_grouple_reader_url(chapter_url) and mtr_url != chapter_url:
        candidate_urls.append(mtr_url)

    last_url = chapter_url
    for access_url in candidate_urls:
        last_url = access_url
        if access_url != chapter_url:
            log.info("Обнаружен возрастной/закрытый ридер. Пробую URL продолжения: %s", access_url)

        soup = fetch(access_url)
        if not soup:
            continue

        result = []
        seen = set()
        imgs = soup.select("img.manga-img")
        for img in imgs:
            # В реальной разметке URL страницы находится в src.
            url = (
                img.get("src")
                or img.get("currentSrc")
                or img.get("data-src")
                or img.get("data-original")
            )
            if not url or url.startswith("data:image/"):
                continue
            url = _clean_url(url, access_url)
            if not url or not url.startswith(("http://", "https://")):
                continue
            if url in seen:
                continue
            seen.add(url)
            result.append(url)

        log.info("HTML img.manga-img: найдено страниц: %s", len(result))
        for n, u in enumerate(result[:10], 1):
            log.info("  страница %03d: %s", n, u)
        if len(result) > 10:
            log.info("  ... ещё %s страниц", len(result) - 10)

        if result:
            return result, access_url

        if _looks_like_age_gate(soup) and access_url == chapter_url:
            log.info("На странице обнаружено возрастное предупреждение; следующий запрос будет с ?mtr=true")

    # Selenium оставляем только как последний fallback для обычных динамических ридеров.
    if SELENIUM_AVAILABLE:
        if last_url != chapter_url:
            log.info("Статический HTML с mtr=true пуст, пробую браузер: %s", last_url)
        else:
            log.info("Статический HTML пуст, пробую получить страницы через Selenium: %s", chapter_url)
        result = _selenium_chapter_images(last_url)
        if result:
            return result, last_url
    else:
        log.warning("Не удалось получить страницы ридера статически, Selenium недоступен")

    return [], last_url

def _extension_from_url(url: str, content_type: str = "") -> str:
    ext = Path(urlparse(url).path).suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return ".jpg" if ext == ".jpeg" else ext
    ctype = content_type.lower()
    if "png" in ctype: return ".png"
    if "webp" in ctype: return ".webp"
    if "gif" in ctype: return ".gif"
    return ".jpg"


def download_image(url: str, destination: Path, referer: str) -> bool:
    """Скачивает страницу, но не сохраняет HTML/404 как картинку."""
    if destination.exists() and destination.stat().st_size > 0:
        return True
    headers = dict(HEADERS)
    headers["Referer"] = referer
    try:
        headers["Origin"] = f"{urlparse(referer).scheme}://{urlparse(referer).netloc}"
    except Exception:
        pass
    for attempt in range(1, 4):
        try:
            r = requests.get(url, headers=headers, timeout=DOWNLOAD_TIMEOUT, stream=True)
            if r.status_code == 404:
                log.warning("Изображение не существует (404): %s", url)
                return False
            r.raise_for_status()
            content_type = r.headers.get("Content-Type", "").lower()
            if not content_type.startswith("image/"):
                log.warning("Пропущен не-image ответ %s (%s): %s", content_type, r.status_code, url)
                return False
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("wb") as f:
                for chunk in r.iter_content(64 * 1024):
                    if chunk:
                        f.write(chunk)
            if destination.stat().st_size == 0:
                destination.unlink(missing_ok=True)
                raise IOError("пустой файл")
            return True
        except (requests.RequestException, OSError) as e:
            log.warning("Ошибка загрузки %s (попытка %s/3): %s", url, attempt, e)
            if attempt < 3:
                time.sleep(attempt)
    return False


def download_cover(cover_url: str | None, manga_title: str, referer: str) -> str | None:
    """Скачивает обложку локально в covers/<манга>/cover.<ext> и возвращает
    относительный путь к файлу (или None, если скачать не удалось/нет ссылки).

    По требованию: обложка хранится в БД как локальный путь, а не как ссылка.
    """
    if not cover_url:
        return None
    ext = _extension_from_url(cover_url)
    path = COVERS_DIR / _safe_name(manga_title) / f"cover{ext}"
    if download_image(cover_url, path, referer):
        return str(path)
    return None


def download_characters(characters: list[dict], manga_title: str, referer: str) -> list[dict]:
    """Скачивает картинки персонажей в characters/<манга>/<имя>.<ext>.

    Возвращает список с исходной ссылкой и локальным путём изображения для
    последующего сохранения в таблицу characters.
    """
    folder = CHARACTERS_DIR / _safe_name(manga_title)
    result = []
    for char in characters:
        url = char.get("image_url")
        entry = {"name": char.get("name"), "image_url": url, "image": None}
        if url:
            ext = _extension_from_url(url)
            path = folder / f"{_safe_name(char.get('name', ''))}{ext}"
            if download_image(url, path, referer):
                entry["image"] = str(path)
        result.append(entry)
    return result


def download_chapter(chapter: dict, manga_title: str, index: int,
                     manga_id: int | None = None) -> int:
    """Сохраняет все страницы одной главы и manifest.json."""
    number = _chapter_number(chapter["title"], chapter["url"])
    number_part = f"{number:g}" if number != float("inf") else str(index)
    folder_name = f"chapter_{index:03d}_{_chapter_key(chapter['title'], chapter['url'])}"
    folder = CHAPTERS_DIR / _safe_name(manga_title) / folder_name
    folder.mkdir(parents=True, exist_ok=True)

    image_urls, image_page_url = parse_chapter_images(chapter["url"])
    if not image_urls:
        log.warning("В главе не найдены изображения: %s", chapter["url"])
        return 0

    manifest = {"title": chapter["title"], "number": number_part,
                "chapter_url": chapter["url"],
                "access_url": image_page_url,
                "images": []}
    downloaded = 0
    saved_pages = []
    for page_no, image_url in enumerate(image_urls, start=1):
        ext = _extension_from_url(image_url)
        path = folder / f"{page_no:04d}{ext}"
        if download_image(image_url, path, image_page_url):
            downloaded += 1
            manifest["images"].append({"file": path.name, "url": image_url})
            saved_pages.append({"number": page_no, "url": image_url, "file_path": str(path)})
        time.sleep(0.2)

    (folder / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    save_chapter_pages(
        manga_id,
        {**chapter, "number": number_part, "index": index},
        saved_pages,
    )
    return downloaded


def parse_first_chapters(manga_url: str, manga_title: str, chapters_count: int = 1,
                         manga_id: int | None = None) -> tuple[int, int]:
    """Парсит первые chapters_count глав выбранной манги.

    Возвращает (сколько_страниц_скачано, сколько_всего_глав_найдено) —
    второе число используется, чтобы сохранить в БД реальное число глав.
    """
    chapters = parse_chapter_links(manga_url)
    if not chapters:
        log.warning("Главы не найдены: %s", manga_url)
        return 0, 0

    selected = chapters[:max(1, chapters_count)]
    log.info("%s: найдено глав %s, скачиваю первые %s", manga_title, len(chapters), len(selected))
    total = 0
    for index, chapter in enumerate(selected, start=1):
        log.info("Глава %s: %s", index, chapter["title"])
        total += download_chapter(chapter, manga_title, index, manga_id)
        time.sleep(0.7)
    return total, len(chapters)


# ---------------------------------------------------------------------------
# 5. Основной цикл
# ---------------------------------------------------------------------------

def load_urls_from_file(path: str) -> list[tuple[str, str]]:
    """Читает urls.txt. Можно указывать просто URL или source;URL.

    Примеры:
        https://1.seimanga.me/spokoinoi_nochi__punpun
        https://zazaza.me/manga/example
        seimanga;https://1.seimanga.me/spokoinoi_nochi__punpun
    """
    pairs = []
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            for line_num, raw_line in enumerate(f, start=1):
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue

                if ";" in line:
                    source, url = line.split(";", 1)
                    source = source.strip().lower()
                    url = url.strip()
                else:
                    url = line
                    host = urlparse(url).netloc.lower()
                    if "seimanga" in host:
                        source = "seimanga"
                    elif "zazaza" in host:
                        source = "zazaza"
                    else:
                        log.warning("Строка %s пропущена: не удалось определить сайт по URL: %s", line_num, url)
                        continue

                if source not in ("seimanga", "zazaza"):
                    log.warning("Строка %s: неизвестный источник '%s'", line_num, source)
                    continue
                if not url.startswith(("http://", "https://")):
                    log.warning("Строка %s пропущена: URL должен начинаться с http:// или https://", line_num)
                    continue
                pairs.append((source, url))
    except FileNotFoundError:
        log.error("Файл со ссылками не найден: %s", path)
    return pairs

def run(urls_file: str = "urls.txt", chapters_count: int = 1):
    """Главный режим: берёт прямые URL из urls.txt и скачивает первые N глав."""
    init_db()

    pairs = load_urls_from_file(urls_file)
    log.info("Загружено ссылок из %s: %s", urls_file, len(pairs))
    if not pairs:
        log.warning("В urls.txt нет корректных ссылок. Добавьте URL, по одному в строке.")
        return

    for source, url in pairs:
        log.info("Парсинг [%s]: %s", source, url)
        item = make_empty_item(source, url)
        item = parse_grouple_detail(item)

        actual_title = item.get("title") or Path(urlparse(url).path).name or "manga"
        if not item.get("title"):
            log.warning("Название не найдено, использую имя из URL: %s", actual_title)

        # Обложка и персонажи скачиваются локально (не хранятся как ссылки).
        item["cover_path"] = download_cover(item.get("cover_url"), actual_title, url)
        item["characters"] = download_characters(item.get("characters") or [], actual_title, url)

        manga_id = save_manga(item)
        save_characters(manga_id, item.get("characters") or [])
        _, total_chapters = parse_first_chapters(url, actual_title, chapters_count, manga_id)
        item["chapters_count"] = total_chapters
        save_manga(item)
        time.sleep(1.5)

    log.info("Готово. Обработано ссылок: %s", len(pairs))


if __name__ == "__main__":
    # Единственный основной режим: ссылки берутся из urls.txt.
    # Только первая глава каждой манги скачивается локально в папку chapters/.
    run("urls.txt", chapters_count=1)
    # Например, для первых 10 глав: run("urls.txt", chapters_count=10)
