import argparse
import os
import re
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import psycopg2
import requests
from bs4 import BeautifulSoup

BASE = "https://mangarw.com"
START_URL = "https://mangarw.com/browse?sort=views_month&status=&isAdult=true"
REQUEST_DELAY = float(os.environ.get("CRAWLER_DELAY", "0.25"))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 Chrome/136 Safari/537.36"
    )
}


def get_conn():
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
    )


def create_tables(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS manga_titles (
                id BIGSERIAL PRIMARY KEY,
                href TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                src TEXT,
                source_url TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS manga_details (
                manga_title_id BIGINT PRIMARY KEY,
                description TEXT,
                crawled_at TIMESTAMPTZ,
                images_crawled_at TIMESTAMPTZ,
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
                source_id BIGINT UNIQUE,
                name TEXT NOT NULL,
                href TEXT UNIQUE NOT NULL,
                chapter_number NUMERIC,
                source_published_at TEXT,
                crawled_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            ALTER TABLE manga_chapters
            ADD COLUMN IF NOT EXISTS crawled_at TIMESTAMPTZ;

            CREATE INDEX IF NOT EXISTS manga_chapters_title_number_idx
            ON manga_chapters (
                manga_title_id,
                chapter_number DESC NULLS LAST,
                id DESC
            );

            CREATE TABLE IF NOT EXISTS chapter_images (
                id BIGSERIAL PRIMARY KEY,
                chapter_id BIGINT NOT NULL
                    REFERENCES manga_chapters(id) ON DELETE CASCADE,
                position INTEGER NOT NULL CHECK (position >= 0),
                src TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (chapter_id, position),
                UNIQUE (chapter_id, src)
            );

            CREATE INDEX IF NOT EXISTS chapter_images_chapter_position_idx
            ON chapter_images (chapter_id, position);
        """)
    conn.commit()


def get_soup(url):
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def get_img_src(img, base_url=BASE):
    src = img.get("data-src") or img.get("data-original") or img.get("src")
    if not src or src.startswith("data:image"):
        return None
    return urljoin(base_url, src)


def build_page_url(page):
    parsed = urlparse(START_URL)
    query = parse_qs(parsed.query)
    query["page"] = [str(page)]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def get_max_page(soup):
    max_page = 1
    for anchor in soup.select('a[href*="page="]'):
        query = parse_qs(urlparse(anchor.get("href", "")).query)
        try:
            max_page = max(max_page, int(query.get("page", ["1"])[0]))
        except ValueError:
            pass
    return max_page


def crawl_page(page):
    url = build_page_url(page)
    soup = get_soup(url)
    rows = {}

    for anchor in soup.select('a[href^="/manga/"][title]'):
        image = anchor.select_one("img")
        if not image:
            continue

        href = urljoin(BASE, anchor.get("href", ""))
        title = anchor.get("title", "").strip()
        src = get_img_src(image)
        if title and src:
            rows[href] = {
                "href": href,
                "title": title,
                "src": src,
                "source_url": START_URL,
            }

    return list(rows.values())


def upsert_titles(conn, rows):
    with conn.cursor() as cur:
        for row in rows:
            cur.execute("""
                INSERT INTO manga_titles (href, title, src, source_url)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (href) DO UPDATE SET
                    title = EXCLUDED.title,
                    src = EXCLUDED.src,
                    source_url = EXCLUDED.source_url,
                    updated_at = NOW();
            """, (
                row["href"],
                row["title"],
                row["src"],
                row["source_url"],
            ))
    conn.commit()


def chapter_number(name):
    match = re.search(r"(\d+(?:\.\d+)?)", name)
    return match.group(1) if match else None


def source_id_from_href(href):
    try:
        return int(parse_qs(urlparse(href).query)["id"][0])
    except (KeyError, IndexError, ValueError):
        return None


def crawl_manga_detail(url):
    soup = get_soup(url)
    description_tag = soup.select_one('meta[name="description"]')
    description = description_tag.get("content", "").strip() if description_tag else None
    chapters = {}

    chapter_anchors = soup.select(
        'li[data-chapter-title] a[href*="/read?id="]'
    )
    if not chapter_anchors:
        chapter_anchors = soup.select('a[href*="/read?id="]')

    for anchor in chapter_anchors:
        href = urljoin(BASE, anchor.get("href", ""))
        list_item = anchor.find_parent("li")
        heading = anchor.select_one("h4")
        name = (
            list_item.get("data-chapter-title", "").strip()
            if list_item else ""
        )
        if not name and heading:
            name = heading.get_text(" ", strip=True)
        if not name:
            name = anchor.get_text(" ", strip=True)
        if not name or not href:
            continue

        published = None
        if list_item:
            date_spans = list_item.select("span")
            for span in reversed(date_spans):
                text = span.get_text(" ", strip=True)
                if re.search(r"\d{4}年\d{1,2}月\d{1,2}日", text):
                    published = text
                    break

        chapters[href] = {
            "source_id": source_id_from_href(href),
            "name": name,
            "href": href,
            "chapter_number": chapter_number(name),
            "source_published_at": published,
        }

    return description, list(chapters.values())


def upsert_chapter(conn, manga_title_id, chapter):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO manga_chapters (
                manga_title_id,
                source_id,
                name,
                href,
                chapter_number,
                source_published_at
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (href) DO UPDATE SET
                manga_title_id = EXCLUDED.manga_title_id,
                source_id = EXCLUDED.source_id,
                name = EXCLUDED.name,
                chapter_number = EXCLUDED.chapter_number,
                source_published_at = EXCLUDED.source_published_at,
                updated_at = NOW()
            RETURNING id;
        """, (
            manga_title_id,
            chapter["source_id"],
            chapter["name"],
            chapter["href"],
            chapter["chapter_number"],
            chapter["source_published_at"],
        ))
        chapter_id = cur.fetchone()[0]
    conn.commit()
    return chapter_id


def chapter_has_images(conn, chapter_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM chapter_images WHERE chapter_id = %s)",
            (chapter_id,),
        )
        return cur.fetchone()[0]


def crawl_chapter_images(url):
    soup = get_soup(url)
    images = []
    for image in soup.select("#viewer img.page-img"):
        src = get_img_src(image, url)
        if src and src not in images:
            images.append(src)
    return images


def replace_chapter_images(conn, chapter_id, images):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM chapter_images WHERE chapter_id = %s", (chapter_id,))
        for position, src in enumerate(images):
            cur.execute("""
                INSERT INTO chapter_images (chapter_id, position, src)
                VALUES (%s, %s, %s);
            """, (chapter_id, position, src))
        cur.execute("""
            UPDATE manga_chapters
            SET crawled_at = NOW(), updated_at = NOW()
            WHERE id = %s;
        """, (chapter_id,))
    conn.commit()


def crawl_single_chapter(conn, chapter_id):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, name, href
            FROM manga_chapters
            WHERE id = %s;
        """, (chapter_id,))
        chapter = cur.fetchone()

    if not chapter:
        raise ValueError(f"Chapter {chapter_id} does not exist")

    stored_id, name, href = chapter
    images = crawl_chapter_images(href)
    if not images:
        raise ValueError(f"No images found for {name} ({href})")

    replace_chapter_images(conn, stored_id, images)
    print(f"{name}: saved {len(images)} images", flush=True)
    return len(images)


def get_titles(conn, manga_id=None):
    with conn.cursor() as cur:
        if manga_id is None:
            cur.execute("""
                SELECT id, title, href
                FROM manga_titles
                ORDER BY id;
            """)
        else:
            cur.execute("""
                SELECT id, title, href
                FROM manga_titles
                WHERE id = %s;
            """, (manga_id,))
        return cur.fetchall()


def crawl_all_titles(conn):
    first_soup = get_soup(START_URL)
    max_page = get_max_page(first_soup)
    print(f"Browse pages: {max_page}", flush=True)

    for page in range(1, max_page + 1):
        rows = crawl_page(page)
        upsert_titles(conn, rows)
        print(
            f"Titles page {page}/{max_page}: upserted {len(rows)}",
            flush=True,
        )
        time.sleep(REQUEST_DELAY)


def crawl_library(
    conn,
    manga_id=None,
    max_chapters=None,
    crawl_images=False,
):
    titles = get_titles(conn, manga_id)
    print(f"Manga details to crawl: {len(titles)}", flush=True)

    for title_index, (title_id, title, href) in enumerate(titles, start=1):
        try:
            if crawl_images:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO manga_details (
                            manga_title_id,
                            crawl_status,
                            crawl_error
                        )
                        VALUES (%s, 'crawling', NULL)
                        ON CONFLICT (manga_title_id) DO UPDATE SET
                            crawl_status = 'crawling',
                            crawl_error = NULL,
                            images_crawled_at = NULL,
                            updated_at = NOW();
                    """, (title_id,))
                conn.commit()

            description, chapters = crawl_manga_detail(href)
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO manga_details (
                        manga_title_id,
                        description,
                        crawled_at
                    )
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (manga_title_id) DO UPDATE SET
                        description = EXCLUDED.description,
                        crawled_at = NOW(),
                        updated_at = NOW();
                """, (title_id, description))
            conn.commit()

            chapters.sort(
                key=lambda item: (
                    float(item["chapter_number"])
                    if item["chapter_number"] is not None else -1
                ),
                reverse=True,
            )
            if max_chapters is not None:
                chapters = chapters[:max_chapters]

            print(
                f"[{title_index}/{len(titles)}] {title}: "
                f"{len(chapters)} chapters",
                flush=True,
            )

            stored_chapters = []
            for chapter in chapters:
                stored_id = upsert_chapter(conn, title_id, chapter)
                stored_chapters.append((stored_id, chapter))

            if not crawl_images:
                print(
                    "  Chapter list saved; crawl images from each chapter button",
                    flush=True,
                )
                continue

            if not stored_chapters:
                raise RuntimeError("No chapters found for full title crawl")

            failed_chapters = []
            skipped_chapters = 0
            crawled_chapters = 0
            for chapter_index, (stored_id, chapter) in enumerate(
                stored_chapters,
                start=1,
            ):
                if chapter_has_images(conn, stored_id):
                    skipped_chapters += 1
                    print(
                        f"  [{chapter_index}/{len(stored_chapters)}] "
                        f"{chapter['name']}: skipped (images already exist)",
                        flush=True,
                    )
                    continue

                try:
                    images = crawl_chapter_images(chapter["href"])
                    if not images:
                        raise ValueError("No images found")
                    replace_chapter_images(conn, stored_id, images)
                    crawled_chapters += 1
                    print(
                        f"  [{chapter_index}/{len(stored_chapters)}] "
                        f"{chapter['name']}: saved {len(images)} images",
                        flush=True,
                    )
                except Exception as error:
                    conn.rollback()
                    failed_chapters.append(chapter["name"])
                    print(
                        f"  ERROR {chapter['name']} ({chapter['href']}): "
                        f"{error}",
                        flush=True,
                    )

                time.sleep(REQUEST_DELAY)

            if failed_chapters:
                raise RuntimeError(
                    f"{len(failed_chapters)} chapter(s) failed: "
                    + ", ".join(failed_chapters[:5])
                )

            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE manga_details
                    SET
                        images_crawled_at = NOW(),
                        crawl_status = %s,
                        crawl_error = NULL,
                        updated_at = NOW()
                    WHERE manga_title_id = %s;
                """, (
                    "completed" if crawled_chapters > 0 else "no_changes",
                    title_id,
                ))
            conn.commit()
            print(
                "  Incremental title crawl completed: "
                f"{crawled_chapters} crawled, "
                f"{skipped_chapters} skipped",
                flush=True,
            )
        except Exception as error:
            conn.rollback()
            if crawl_images:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE manga_details
                        SET
                            crawl_status = 'failed',
                            crawl_error = %s,
                            updated_at = NOW()
                        WHERE manga_title_id = %s;
                    """, (str(error), title_id))
                conn.commit()
            print(f"ERROR {title} ({href}): {error}", flush=True)
            if manga_id is not None and crawl_images:
                raise

        time.sleep(REQUEST_DELAY)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--manga-id",
        type=int,
        help="Only crawl the local manga_titles.id supplied",
    )
    parser.add_argument(
        "--max-chapters",
        type=int,
        help="Limit chapters per manga for a test run",
    )
    parser.add_argument(
        "--skip-title-list",
        action="store_true",
        help="Do not refresh the browse/title list first",
    )
    parser.add_argument(
        "--chapter-id",
        type=int,
        help="Crawl images for one local manga_chapters.id",
    )
    parser.add_argument(
        "--crawl-images",
        action="store_true",
        help="Crawl images for every chapter in the selected title(s)",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    conn = get_conn()
    try:
        create_tables(conn)
        if args.chapter_id is not None:
            crawl_single_chapter(conn, args.chapter_id)
            return

        if not args.skip_title_list and args.manga_id is None:
            crawl_all_titles(conn)
        crawl_library(
            conn,
            manga_id=args.manga_id,
            max_chapters=args.max_chapters,
            crawl_images=args.crawl_images,
        )
    finally:
        conn.close()
    print("Crawler finished", flush=True)


if __name__ == "__main__":
    main()
