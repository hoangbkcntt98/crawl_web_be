Run
## Database schema

Apply the manga-web `db/` migrations during deployment before starting the web
app or this crawler. Runtime crawling never creates tables, indexes, or columns.
If a required table or column is missing, stop the crawl and migrate the database
instead of modifying the schema from crawler code.

## Run with config stored in the database

Register the JSON config in the `crawler_sites` table from the Next.js app,
then use its `site_key`:

```bash
python generic_manga_crawler.py --site-key mangarw
```

The original file-based mode remains available:

```bash
python generic_manga_crawler.py --config mangarw.config.json
```

Crawl only title + chapter list:

python generic_manga_crawler.py --site-key mangarw

Crawl images too:

python generic_manga_crawler.py --site-key mangarw --crawl-images

Test one manga:

python generic_manga_crawler.py --site-key mangarw --manga-id 1 --max-chapters 3 --crawl-images

Crawl one chapter:

python generic_manga_crawler.py --site-key mangarw --chapter-id 10
