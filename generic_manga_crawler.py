import argparse
import json
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import psycopg2
import requests
from bs4 import BeautifulSoup

# Generic manga crawler entrypoint.
#
# Runtime flow:
# 1. Load a site config either from --config JSON or crawler_sites by --site-key.
# 2. Ensure DB tables/indexes exist.
# 3. Optionally refresh title list from the configured browse pages.
# 4. Crawl each title detail page to upsert chapter rows.
# 5. If --crawl-images or --chapter-id is supplied, crawl reader images too.
#
# This file intentionally keeps site-specific selectors in JSON config so the
# Next.js app can register multiple sites without adding Python code per site.

REQUEST_DELAY = float(os.environ.get("CRAWLER_DELAY", "0.25"))

HEADERS = {
    "User-Agent": os.environ.get(
        "CRAWLER_USER_AGENT",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/136 Safari/537.36",
    )
}

DEFAULT_IMAGE_ATTRS = ["data-src", "data-original", "data-lazy-src", "src"]
IMAGE_STORAGE_DIR = Path(
    os.environ.get("MANGA_IMAGE_STORAGE", "/home/opc/manga-storage")
).resolve()


def validate_config(config):
    required = [
        "site_key",
        "base_url",
        "list.url",
        "list.item_selector",
        "list.title",
        "list.href",
        "list.image",
        "detail.chapters.link_selector",
        "detail.chapters.title_sources",
        "reader.image_selector",
    ]
    for key in required:
        node = config
        for part in key.split("."):
            if part not in node:
                raise ValueError(f"Missing config: {key}")
            node = node[part]
    return config


def load_config(path):
    """Load crawler settings from a JSON file for local/manual runs."""
    with open(path, "r", encoding="utf-8") as file:
        config = json.load(file)
    config["_store_images_locally"] = bool(config.get("store_images_locally"))
    config["_local_image_storage_path"] = config.get("local_image_storage_path")
    return validate_config(config)


def load_config_from_db(conn, site_key):
    """Load crawler settings registered from the Next.js config UI."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT config, store_images_locally, local_image_storage_path
            FROM crawler_sites
            WHERE site_key = %s
            """,
            (site_key,),
        )
        row = cur.fetchone()

    if not row:
        raise ValueError(f"Site config '{site_key}' does not exist")

    config = row[0]
    if isinstance(config, str):
        config = json.loads(config)
    if config.get("site_key") != site_key:
        raise ValueError("Stored config site_key does not match the selected site")
    config["_store_images_locally"] = bool(row[1])
    config["_local_image_storage_path"] = row[2]
    return validate_config(config)


def get_conn():
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
    )


def create_tables(conn):
    """Create or migrate the crawler-owned schema before every run."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtext('generic_manga_crawler.create_tables'))"
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS manga_titles (
                id BIGSERIAL PRIMARY KEY,
                site_key TEXT NOT NULL DEFAULT 'default',
                href TEXT NOT NULL,
                title TEXT NOT NULL,
                src TEXT,
                source_url TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            ALTER TABLE manga_titles
            ADD COLUMN IF NOT EXISTS site_key TEXT NOT NULL DEFAULT 'default';

            CREATE INDEX IF NOT EXISTS manga_titles_site_key_idx
            ON manga_titles (site_key);

            ALTER TABLE manga_titles
            DROP CONSTRAINT IF EXISTS manga_titles_href_key;

            CREATE UNIQUE INDEX IF NOT EXISTS manga_titles_site_href_key
            ON manga_titles (site_key, href);

            CREATE TABLE IF NOT EXISTS crawler_sites (
                id BIGSERIAL PRIMARY KEY,
                site_key TEXT NOT NULL UNIQUE,
                config JSONB NOT NULL,
                store_images_locally BOOLEAN NOT NULL DEFAULT FALSE,
                local_image_storage_path TEXT,
                crawl_status TEXT NOT NULL DEFAULT 'idle',
                crawl_error TEXT,
                crawler_pid INTEGER,
                crawl_started_at TIMESTAMPTZ,
                last_crawled_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CHECK (jsonb_typeof(config) = 'object')
            );

            ALTER TABLE crawler_sites
            ADD COLUMN IF NOT EXISTS crawler_pid INTEGER;

            ALTER TABLE crawler_sites
            ADD COLUMN IF NOT EXISTS crawl_started_at TIMESTAMPTZ;

            ALTER TABLE crawler_sites
            ADD COLUMN IF NOT EXISTS store_images_locally BOOLEAN NOT NULL DEFAULT FALSE;

            ALTER TABLE crawler_sites
            ADD COLUMN IF NOT EXISTS local_image_storage_path TEXT;

            CREATE TABLE IF NOT EXISTS manga_details (
                manga_title_id BIGINT PRIMARY KEY,
                description TEXT,
                crawled_at TIMESTAMPTZ,
                images_crawled_at TIMESTAMPTZ,
                crawl_status TEXT NOT NULL DEFAULT 'idle',
                crawl_error TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            ALTER TABLE manga_details
            ADD COLUMN IF NOT EXISTS crawled_at TIMESTAMPTZ;

            ALTER TABLE manga_details
            ADD COLUMN IF NOT EXISTS images_crawled_at TIMESTAMPTZ;

            ALTER TABLE manga_details
            ADD COLUMN IF NOT EXISTS crawl_status TEXT NOT NULL DEFAULT 'idle';

            ALTER TABLE manga_details
            ADD COLUMN IF NOT EXISTS crawl_error TEXT;

            CREATE TABLE IF NOT EXISTS manga_chapters (
                id BIGSERIAL PRIMARY KEY,
                manga_title_id BIGINT NOT NULL,
                source_id BIGINT,
                name TEXT NOT NULL,
                href TEXT NOT NULL,
                chapter_number NUMERIC,
                source_published_at TEXT,
                crawled_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            ALTER TABLE manga_chapters
            ADD COLUMN IF NOT EXISTS crawled_at TIMESTAMPTZ;

            CREATE INDEX IF NOT EXISTS manga_chapters_source_id_idx
            ON manga_chapters (source_id)
            WHERE source_id IS NOT NULL;

            CREATE UNIQUE INDEX IF NOT EXISTS manga_chapters_title_source_id_key
            ON manga_chapters (manga_title_id, source_id)
            WHERE source_id IS NOT NULL;

            CREATE UNIQUE INDEX IF NOT EXISTS manga_chapters_title_href_path_key
            ON manga_chapters (
                manga_title_id,
                regexp_replace(href, '^https?://[^/]+', '', 'i')
            );

            ALTER TABLE manga_chapters
            DROP CONSTRAINT IF EXISTS manga_chapters_href_key;

            CREATE UNIQUE INDEX IF NOT EXISTS manga_chapters_title_href_key
            ON manga_chapters (manga_title_id, href);

            CREATE INDEX IF NOT EXISTS manga_chapters_title_number_idx
            ON manga_chapters (
                manga_title_id,
                chapter_number DESC NULLS LAST,
                id DESC
            );

            CREATE TABLE IF NOT EXISTS chapter_images (
                id BIGSERIAL PRIMARY KEY,
                chapter_id BIGINT NOT NULL REFERENCES manga_chapters(id) ON DELETE CASCADE,
                position INTEGER NOT NULL CHECK (position >= 0),
                src TEXT NOT NULL,
                local_path TEXT,
                content_type TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (chapter_id, position),
                UNIQUE (chapter_id, src)
            );

            CREATE INDEX IF NOT EXISTS chapter_images_chapter_position_idx
            ON chapter_images (chapter_id, position);

            ALTER TABLE chapter_images
            ADD COLUMN IF NOT EXISTS local_path TEXT;

            ALTER TABLE chapter_images
            ADD COLUMN IF NOT EXISTS content_type TEXT;

            CREATE INDEX IF NOT EXISTS chapter_images_local_path_idx
            ON chapter_images (local_path)
            WHERE local_path IS NOT NULL;
            """
        )
    conn.commit()


def reset_tables(conn):
    """Delete all crawler data and reset ID sequences.

    This is intentionally global, not site_key-scoped, because --reset means
    start from a completely clean crawler database.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            TRUNCATE TABLE
                chapter_images,
                manga_chapters,
                manga_details,
                manga_titles
            RESTART IDENTITY CASCADE;
            """
        )
    conn.commit()
    print("Reset completed: all crawler tables were truncated", flush=True)


def get_soup(url):
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def absolute_url(base_url, maybe_url):
    if not maybe_url:
        return None
    return urljoin(base_url, maybe_url.strip())


def first_attr(element, attrs):
    if isinstance(attrs, str):
        attrs = [attrs]
    for attr in attrs:
        value = element.get(attr)
        if value:
            return value.strip()
    return None


def select_scope(scope, selector):
    if selector in (None, "@self", "self"):
        return scope
    return scope.select_one(selector)


def extract_value(scope, spec, base_url):
    """
    Generic field extractor.

    Supported spec examples:
    {"selector": "@self", "attr": "href", "absolute_url": true}
    {"selector": "img", "attr": ["data-src", "src"], "absolute_url": true}
    {"selector": "h4", "text": true}
    """
    element = select_scope(scope, spec.get("selector", "@self"))
    if not element:
        return None

    if spec.get("text"):
        value = element.get_text(" ", strip=True)
    else:
        value = first_attr(element, spec.get("attr", "href"))

    if not value or value.startswith("data:image"):
        return None

    if spec.get("regex"):
        match = re.search(spec["regex"], value)
        value = match.group(1) if match and match.groups() else match.group(0) if match else None

    if value and spec.get("absolute_url"):
        value = absolute_url(base_url, value)

    return value.strip() if isinstance(value, str) else value


def extract_first_value(scope, specs, base_url):
    for spec in specs:
        value = extract_value(scope, spec, base_url)
        if value:
            return value
    return None


def extract_image_src(image, base_url, attrs=None):
    src = first_attr(image, attrs or DEFAULT_IMAGE_ATTRS)
    if not src or src.startswith("data:image"):
        return None
    return absolute_url(base_url, src)


def build_page_url(list_config, page):
    start_url = list_config["url"]

    if list_config.get("page_url_format"):
        return list_config["page_url_format"].format(page=page)

    page_param = list_config.get("page_param", "page")
    parsed = urlparse(start_url)
    query = parse_qs(parsed.query)
    query[page_param] = [str(page)]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def get_max_page(soup, list_config):
    if list_config.get("max_pages"):
        return int(list_config["max_pages"])

    selector = list_config.get("pagination_selector", 'a[href*="page="]')
    page_param = list_config.get("page_param", "page")
    max_page = 1

    for anchor in soup.select(selector):
        href = anchor.get("href", "")
        query = parse_qs(urlparse(href).query)
        candidates = query.get(page_param, [])

        if not candidates:
            text = anchor.get_text(" ", strip=True)
            candidates = [text]

        for candidate in candidates:
            try:
                max_page = max(max_page, int(candidate))
            except ValueError:
                pass

    return max_page


def parse_crawl_pages(config, max_page):
    """Return explicit title-list pages from crawl_page, or all pages when omitted."""
    raw_pages = config.get("crawl_page")
    if raw_pages is None:
        raw_pages = config.get("list", {}).get("crawl_page")
    if raw_pages in (None, "", []):
        return list(range(1, max_page + 1))

    if isinstance(raw_pages, int):
        candidates = [raw_pages]
    elif isinstance(raw_pages, str):
        candidates = [part.strip() for part in raw_pages.split(",")]
    elif isinstance(raw_pages, list):
        candidates = raw_pages
    else:
        raise ValueError("crawl_page must be a number, array, or comma-separated string")

    pages = []
    for value in candidates:
        if value in (None, ""):
            continue
        try:
            page = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"Invalid crawl_page value: {value!r}") from None
        if page < 1:
            raise ValueError(f"crawl_page must be greater than 0: {page}")
        if page not in pages:
            pages.append(page)

    return pages or list(range(1, max_page + 1))


def crawl_title_page(config, page):
    list_config = config["list"]
    url = build_page_url(list_config, page)
    soup = get_soup(url)
    rows = {}

    for item in soup.select(list_config["item_selector"]):
        href = extract_value(item, list_config["href"], config["base_url"])
        title = extract_value(item, list_config["title"], config["base_url"])
        image_src = extract_value(item, list_config["image"], config["base_url"])

        if href and title:
            rows[href] = {
                "site_key": config["site_key"],
                "href": href,
                "title": title,
                "src": image_src,
                "source_url": url,
            }

    return list(rows.values())


def upsert_titles(conn, rows):
    with conn.cursor() as cur:
        for row in rows:
            cur.execute(
                """
                INSERT INTO manga_titles (site_key, href, title, src, source_url)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (site_key, href) DO UPDATE SET
                    title = EXCLUDED.title,
                    src = EXCLUDED.src,
                    source_url = EXCLUDED.source_url,
                    updated_at = NOW();
                """,
                (
                    row["site_key"],
                    row["href"],
                    row["title"],
                    row["src"],
                    row["source_url"],
                ),
            )
    conn.commit()


def chapter_number(name):
    match = re.search(r"(\d+(?:\.\d+)?)", name or "")
    return match.group(1) if match else None


def source_id_from_href(href, query_param=None, regex=None):
    if regex:
        match = re.search(regex, href)
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                return None

    if query_param:
        try:
            return int(parse_qs(urlparse(href).query)[query_param][0])
        except (KeyError, IndexError, ValueError):
            return None

    return None


def href_path_key(href):
    parsed = urlparse(href)
    if not parsed.scheme or not parsed.netloc:
        return href
    return urlunparse(("", "", parsed.path, "", parsed.query, "")).strip()


def extract_description(soup, detail_config, base_url):
    spec = detail_config.get("description")
    if not spec:
        return None
    return extract_value(soup, spec, base_url)


def crawl_manga_detail(config, url):
    soup = get_soup(url)
    detail_config = config.get("detail", {})
    chapters_config = detail_config["chapters"]
    description = extract_description(soup, detail_config, url)
    chapters = {}

    container_selector = chapters_config.get("container_selector")
    if container_selector:
        containers = soup.select(container_selector)
    else:
        containers = soup.select(chapters_config["link_selector"])

    for container in containers:
        link_scope = container.select_one(chapters_config["link_selector"]) if container_selector else container
        if not link_scope:
            continue

        href = extract_value(
            link_scope,
            chapters_config.get("href", {"selector": "@self", "attr": "href", "absolute_url": True}),
            config["base_url"],
        )
        if not href:
            continue

        title_specs = []
        for raw_spec in chapters_config["title_sources"]:
            spec = dict(raw_spec)
            target = spec.pop("target", "container")
            if target == "link":
                value = extract_value(link_scope, spec, config["base_url"])
            else:
                value = extract_value(container, spec, config["base_url"])
            if value:
                title_specs.append(value)

        name = title_specs[0] if title_specs else None
        if not name:
            continue

        published = None
        published_config = chapters_config.get("published")
        if published_config:
            published_regex = published_config.get("regex")
            for element in container.select(published_config.get("selector", "span")):
                text = element.get_text(" ", strip=True)
                if not published_regex or re.search(published_regex, text):
                    published = text
                    break

        chapters[href] = {
            "source_id": source_id_from_href(
                href,
                query_param=chapters_config.get("source_id_query_param"),
                regex=chapters_config.get("source_id_regex"),
            ),
            "name": name,
            "href": href,
            "chapter_number": chapter_number(name),
            "source_published_at": published,
        }

    return description, list(chapters.values())


def upsert_chapter(conn, manga_title_id, chapter):
    with conn.cursor() as cur:
        if chapter["source_id"] is not None:
            cur.execute(
                """
                SELECT id
                FROM manga_chapters
                WHERE manga_title_id = %s
                  AND source_id = %s
                ORDER BY id ASC
                LIMIT 1;
                """,
                (manga_title_id, chapter["source_id"]),
            )
            existing = cur.fetchone()
            if existing:
                chapter_id = existing[0]
                cur.execute(
                    """
                    UPDATE manga_chapters
                    SET name = %s,
                        href = %s,
                        chapter_number = %s,
                        source_published_at = %s,
                        updated_at = NOW()
                    WHERE id = %s;
                    """,
                    (
                        chapter["name"],
                        chapter["href"],
                        chapter["chapter_number"],
                        chapter["source_published_at"],
                        chapter_id,
                    ),
                )
                conn.commit()
                return chapter_id

        cur.execute(
            """
            SELECT id
            FROM manga_chapters
            WHERE manga_title_id = %s
              AND regexp_replace(href, '^https?://[^/]+', '', 'i') = %s
            ORDER BY id ASC
            LIMIT 1;
            """,
            (manga_title_id, href_path_key(chapter["href"])),
        )
        existing = cur.fetchone()
        if existing:
            chapter_id = existing[0]
            cur.execute(
                """
                UPDATE manga_chapters
                SET source_id = %s,
                    name = %s,
                    href = %s,
                    chapter_number = %s,
                    source_published_at = %s,
                    updated_at = NOW()
                WHERE id = %s;
                """,
                (
                    chapter["source_id"],
                    chapter["name"],
                    chapter["href"],
                    chapter["chapter_number"],
                    chapter["source_published_at"],
                    chapter_id,
                ),
            )
            conn.commit()
            return chapter_id

        cur.execute(
            """
            INSERT INTO manga_chapters (
                manga_title_id,
                source_id,
                name,
                href,
                chapter_number,
                source_published_at
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (manga_title_id, href) DO UPDATE SET
                source_id = EXCLUDED.source_id,
                name = EXCLUDED.name,
                chapter_number = EXCLUDED.chapter_number,
                source_published_at = EXCLUDED.source_published_at,
                updated_at = NOW()
            RETURNING id;
            """,
            (
                manga_title_id,
                chapter["source_id"],
                chapter["name"],
                chapter["href"],
                chapter["chapter_number"],
                chapter["source_published_at"],
            ),
        )
        chapter_id = cur.fetchone()[0]
    conn.commit()
    return chapter_id


def chapter_has_images(conn, config, chapter_id):
    local_required = bool(config.get("_store_images_locally"))
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM chapter_images
                WHERE chapter_id = %s
                  AND (%s = FALSE OR local_path IS NOT NULL)
            )
            """,
            (chapter_id, local_required),
        )
        has_images = cur.fetchone()[0]
    conn.commit()
    return has_images


def mark_title_no_chapters(conn, title_id):
    """Mark a title crawl as a non-fatal warning when no chapter links match."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE manga_details
            SET crawl_status = 'no_chapters',
                crawl_error = NULL,
                images_crawled_at = NULL,
                updated_at = NOW()
            WHERE manga_title_id = %s;
            """,
            (title_id,),
        )
    conn.commit()


def crawl_chapter_images(config, url):
    soup = get_soup(url)
    reader_config = config["reader"]
    attrs = reader_config.get("image_attrs", DEFAULT_IMAGE_ATTRS)
    selector = reader_config["image_selector"]
    images = []

    for image in soup.select(selector):
        src = extract_image_src(image, url, attrs)
        if src and src not in images:
            images.append(src)

    if not images:
        title = soup.title.get_text(" ", strip=True) if soup.title else "untitled page"
        raise ValueError(
            f"No images matched selector {selector!r} at {url} (page: {title})"
        )

    return images


def image_extension(content_type, source_url):
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    extensions = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/avif": ".avif",
    }
    if media_type in extensions:
        return extensions[media_type], media_type

    suffix = Path(urlparse(source_url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"}:
        return (".jpg" if suffix == ".jpeg" else suffix), media_type
    raise ValueError(f"Unsupported image content type {content_type!r}: {source_url}")


def image_storage_dir_for_config(config):
    configured_path = config.get("_local_image_storage_path")
    if configured_path:
        return Path(configured_path).expanduser().resolve()
    return IMAGE_STORAGE_DIR


def download_chapter_images(config, chapter_id, chapter_url, images):
    chapter_dir = image_storage_dir_for_config(config) / "chapters" / str(chapter_id)
    chapter_dir.mkdir(parents=True, exist_ok=True)
    downloaded = []

    for position, source_url in enumerate(images):
        response = requests.get(
            source_url,
            headers={**HEADERS, "Referer": chapter_url},
            timeout=60,
        )
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if not content_type.lower().startswith("image/"):
            raise ValueError(
                f"Expected image but received {content_type!r}: {source_url}"
            )

        extension, media_type = image_extension(content_type, source_url)
        local_path = chapter_dir / f"{position:05d}{extension}"
        with tempfile.NamedTemporaryFile(
            dir=chapter_dir, prefix=".download-", delete=False
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(response.content)
        temp_path.replace(local_path)
        downloaded.append(
            {
                "src": source_url,
                "local_path": str(local_path),
                "content_type": media_type,
            }
        )

    return downloaded


def replace_chapter_images_for_config(conn, config, chapter_id, chapter_url, images):
    if config.get("_store_images_locally"):
        stored_images = download_chapter_images(config, chapter_id, chapter_url, images)
    else:
        stored_images = [
            {
                "src": source_url,
                "local_path": None,
                "content_type": None,
            }
            for source_url in images
        ]
    with conn.cursor() as cur:
        cur.execute("DELETE FROM chapter_images WHERE chapter_id = %s", (chapter_id,))
        for position, image in enumerate(stored_images):
            cur.execute(
                """
                INSERT INTO chapter_images (
                    chapter_id,
                    position,
                    src,
                    local_path,
                    content_type
                )
                VALUES (%s, %s, %s, %s, %s);
                """,
                (
                    chapter_id,
                    position,
                    image["src"],
                    image["local_path"],
                    image["content_type"],
                ),
            )
        cur.execute(
            """
            UPDATE manga_chapters
            SET crawled_at = NOW(), updated_at = NOW()
            WHERE id = %s;
            """,
            (chapter_id,),
        )
    conn.commit()


def crawl_single_chapter(conn, config, chapter_id):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id, c.name, c.href
            FROM manga_chapters c
            JOIN manga_titles m ON m.id = c.manga_title_id
            WHERE c.id = %s AND m.site_key = %s;
            """,
            (chapter_id, config["site_key"]),
        )
        chapter = cur.fetchone()

    if not chapter:
        raise ValueError(f"Chapter {chapter_id} does not exist")

    stored_id, name, href = chapter
    images = crawl_chapter_images(config, href)
    if not images:
        raise ValueError(f"No images found for {name} ({href})")

    replace_chapter_images_for_config(conn, config, stored_id, href, images)
    print(f"{name}: saved {len(images)} images", flush=True)
    return len(images)


def get_titles(conn, site_key, manga_id=None):
    with conn.cursor() as cur:
        if manga_id is None:
            cur.execute(
                """
                SELECT id, title, href
                FROM manga_titles
                WHERE site_key = %s
                ORDER BY id;
                """,
                (site_key,),
            )
        else:
            cur.execute(
                """
                SELECT id, title, href
                FROM manga_titles
                WHERE id = %s AND site_key = %s;
                """,
                (manga_id, site_key),
            )
        return cur.fetchall()


def crawl_all_titles(conn, config):
    """Crawl browse/list pages and upsert only manga title metadata."""
    first_soup = get_soup(config["list"]["url"])
    max_page = get_max_page(first_soup, config["list"])
    pages = parse_crawl_pages(config, max_page)
    all_pages = list(range(1, max_page + 1))
    if pages == all_pages:
        print(f"Browse pages: {max_page}", flush=True)
    else:
        print(
            f"Browse pages: {len(pages)} selected ({', '.join(map(str, pages))})",
            flush=True,
        )

    for page in pages:
        rows = crawl_title_page(config, page)
        upsert_titles(conn, rows)
        print(f"Titles page {page}/{max_page}: upserted {len(rows)}", flush=True)
        time.sleep(REQUEST_DELAY)


def crawl_library(conn, config, manga_id=None, max_chapters=None, crawl_images=False):
    """Crawl title detail pages, chapter lists, and optionally reader images."""
    titles = get_titles(conn, config["site_key"], manga_id)
    print(f"Manga details to crawl: {len(titles)}", flush=True)

    for title_index, (title_id, title, href) in enumerate(titles, start=1):
        try:
            if crawl_images:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO manga_details (manga_title_id, crawl_status, crawl_error)
                        VALUES (%s, 'crawling', NULL)
                        ON CONFLICT (manga_title_id) DO UPDATE SET
                            crawl_status = 'crawling',
                            crawl_error = NULL,
                            images_crawled_at = NULL,
                            updated_at = NOW();
                        """,
                        (title_id,),
                    )
                conn.commit()

            description, chapters = crawl_manga_detail(config, href)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO manga_details (manga_title_id, description, crawled_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (manga_title_id) DO UPDATE SET
                        description = EXCLUDED.description,
                        crawled_at = NOW(),
                        updated_at = NOW();
                    """,
                    (title_id, description),
                )
            conn.commit()

            chapters.sort(
                key=lambda item: (
                    item["chapter_number"] is None,
                    float(item["chapter_number"]) if item["chapter_number"] is not None else 0,
                ),
            )
            if max_chapters is not None:
                chapters = chapters[:max_chapters]

            print(f"[{title_index}/{len(titles)}] {title}: {len(chapters)} chapters", flush=True)

            stored_chapters = []
            for chapter in chapters:
                stored_id = upsert_chapter(conn, title_id, chapter)
                stored_chapters.append((stored_id, chapter))

            if not crawl_images:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE manga_details
                        SET crawl_status = 'chapters_completed',
                            crawl_error = NULL,
                            updated_at = NOW()
                        WHERE manga_title_id = %s;
                        """,
                        (title_id,),
                    )
                conn.commit()
                print("  Chapter list saved; crawl images later with --chapter-id or --crawl-images", flush=True)
                continue

            if not stored_chapters:
                mark_title_no_chapters(conn, title_id)
                print(
                    "  WARNING No chapters found for full title crawl",
                    flush=True,
                )
                continue

            failed_chapters = []
            skipped_chapters = 0
            crawled_chapters = 0

            for chapter_index, (stored_id, chapter) in enumerate(stored_chapters, start=1):
                if chapter_has_images(conn, config, stored_id):
                    skipped_chapters += 1
                    print(
                        f"  [{chapter_index}/{len(stored_chapters)}] {chapter['name']}: skipped",
                        flush=True,
                    )
                    continue

                try:
                    images = crawl_chapter_images(config, chapter["href"])
                    if not images:
                        raise ValueError("No images found")
                    replace_chapter_images_for_config(
                        conn,
                        config,
                        stored_id,
                        chapter["href"],
                        images,
                    )
                    crawled_chapters += 1
                    print(
                        f"  [{chapter_index}/{len(stored_chapters)}] {chapter['name']}: saved {len(images)} images",
                        flush=True,
                    )
                except Exception as error:
                    conn.rollback()
                    failed_chapters.append(chapter["name"])
                    print(f"  ERROR {chapter['name']} ({chapter['href']}): {error}", flush=True)

                time.sleep(REQUEST_DELAY)

            if failed_chapters:
                raise RuntimeError(
                    f"{len(failed_chapters)} chapter(s) failed: " + ", ".join(failed_chapters[:5])
                )

            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE manga_details
                    SET images_crawled_at = NOW(),
                        crawl_status = %s,
                        crawl_error = NULL,
                        updated_at = NOW()
                    WHERE manga_title_id = %s;
                    """,
                    ("completed" if crawled_chapters > 0 else "no_changes", title_id),
                )
            conn.commit()
            print(
                f"  Incremental title crawl completed: {crawled_chapters} crawled, {skipped_chapters} skipped",
                flush=True,
            )

        except Exception as error:
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE manga_details
                    SET crawl_status = 'failed',
                        crawl_error = %s,
                        updated_at = NOW()
                    WHERE manga_title_id = %s;
                    """,
                    (str(error), title_id),
                )
            conn.commit()
            print(f"ERROR {title} ({href}): {error}", flush=True)
            if manga_id is not None and crawl_images:
                raise

        time.sleep(REQUEST_DELAY)


def parse_args():
    parser = argparse.ArgumentParser()
    config_group = parser.add_mutually_exclusive_group(required=True)
    config_group.add_argument("--config", help="Path to crawler JSON config")
    config_group.add_argument("--site-key", help="Load crawler JSON config from crawler_sites")
    parser.add_argument("--manga-id", type=int, help="Only crawl the local manga_titles.id supplied")
    parser.add_argument("--max-chapters", type=int, help="Limit chapters per manga for a test run")
    parser.add_argument("--skip-title-list", action="store_true", help="Do not refresh the browse/title list first")
    parser.add_argument("--chapter-id", type=int, help="Crawl images for one local manga_chapters.id")
    parser.add_argument("--crawl-images", action="store_true", help="Crawl images for every chapter in the selected title(s)")
    parser.add_argument("--reset", action="store_true", help="Delete all crawler data before crawling")
    return parser.parse_args()


def main():
    args = parse_args()
    conn = get_conn()
    try:
        create_tables(conn)
        config = (
            load_config(args.config)
            if args.config
            else load_config_from_db(conn, args.site_key)
        )

        if args.site_key:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE crawler_sites
                    SET crawl_status = 'crawling',
                        crawl_error = NULL,
                        crawler_pid = %s,
                        crawl_started_at = NOW(),
                        updated_at = NOW()
                    WHERE site_key = %s;
                    """,
                    (os.getpid(), args.site_key),
                )
            conn.commit()

        if args.reset:
            reset_tables(conn)

        if args.chapter_id is not None:
            crawl_single_chapter(conn, config, args.chapter_id)
        else:
            if not args.skip_title_list and args.manga_id is None:
                crawl_all_titles(conn, config)

            crawl_library(
                conn,
                config,
                manga_id=args.manga_id,
                max_chapters=args.max_chapters,
                crawl_images=args.crawl_images,
            )
        if args.site_key:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE crawler_sites
                    SET crawl_status = 'completed',
                        crawl_error = NULL,
                        crawler_pid = NULL,
                        crawl_started_at = NULL,
                        last_crawled_at = NOW(),
                        updated_at = NOW()
                    WHERE site_key = %s;
                    """,
                    (args.site_key,),
                )
            conn.commit()
    except BaseException as error:
        conn.rollback()
        if args.site_key:
            error_name = type(error).__name__
            error_message = str(error) or error_name
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE crawler_sites
                    SET crawl_status = 'failed',
                        crawl_error = %s,
                        crawler_pid = NULL,
                        crawl_started_at = NULL,
                        updated_at = NOW()
                    WHERE site_key = %s;
                    """,
                    (f"{error_name}: {error_message}", args.site_key),
                )
            conn.commit()
        raise
    finally:
        conn.close()

    print("Crawler finished", flush=True)


if __name__ == "__main__":
    main()
