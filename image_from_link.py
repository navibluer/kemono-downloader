import asyncio
from playwright.async_api import async_playwright
from urllib.parse import urljoin
import aiohttp
import aiofiles
import os
import re
import time
from datetime import datetime

IMG_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
MAX_PAGE_CONCURRENCY = 3
MAX_DOWNLOAD_CONCURRENCY = 10
MAX_RETRY = 2
MAX_PAGE_RETRY = 2
BATCH_SIZE = 5
CHUNK_SIZE = 8192  # Stream download chunk size


def is_image_url(url):
    return url.lower().endswith(IMG_EXTENSIONS)


async def spinner(msg="Processing"):
    try:
        while True:
            for char in r"-\|/":
                print(f"\r{msg} {char}", end="", flush=True)
                await asyncio.sleep(0.1)
    except asyncio.CancelledError:
        print("\r" + " " * (len(msg) + 2) + "\r", end="", flush=True)


def parse_datetime_attr(datetime_str):
    """Parse datetime attribute like '2021-05-22T04:53:38' to timestamp"""
    try:
        dt = datetime.fromisoformat(datetime_str.replace('Z', '+00:00'))
        return int(dt.timestamp())
    except (ValueError, TypeError, AttributeError):
        return int(time.time())


async def download_image(
    session,
    url,
    save_dir,
    progress,
    sem,
    failed_images,
    save_path_override=None,
    stats=None,
    is_retry=False,
    mtime=None,  # 新增修改時間參數
):
    async with sem:
        try:
            save_path = save_path_override or os.path.join(
                save_dir, url.split("/")[-1].split("?")[0]
            )

            # 已存在檔案不重抓
            if os.path.exists(save_path):
                progress["done"] += 1
                print(
                    f"\r[{progress['done']}/{progress['total']}] SKIP {os.path.basename(save_path)}"
                )
                if stats:
                    stats["SKIP"] += 1
                return True

            async with session.get(url) as resp:
                if resp.status == 200:
                    async with aiofiles.open(save_path, "wb") as f:
                        # Stream download in chunks to reduce memory usage
                        async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                            await f.write(chunk)

                    # 檢查是否 0 byte
                    file_size = os.path.getsize(save_path)
                    if file_size == 0:
                        raise ValueError("Empty file")

                    # 設定檔案修改時間
                    if mtime:
                        os.utime(save_path, (mtime, mtime))

                    progress["done"] += 1
                    basename = os.path.basename(save_path)
                    print(
                        f"\r[{progress['done']}/{progress['total']}] OK {basename}"
                    )
                    if stats:
                        stats["OK_retry" if is_retry else "OK_first"] += 1
                    return True
                else:
                    raise ValueError(f"HTTP status {resp.status}")

        except (aiohttp.ClientError, ValueError, OSError, asyncio.TimeoutError) as e:
            progress["done"] += 1
            print(f"\r[{progress['done']}/{progress['total']}] ERR {url} {e}")
            failed_images.append(url)
            if is_retry and stats:
                stats["ERR_final"] += 1
            return False


async def get_article_links(page, base_url, filter_month=None):
    """Get article links, optionally filtered by filter_month (year, month)"""
    articles = await page.query_selector_all("article")
    links = []

    # Calculate filter timestamp if filter_month is set
    filter_timestamp = None
    if filter_month:
        year, month = filter_month
        filter_timestamp = int(datetime(year, month, 1).timestamp())

    for article in articles:
        # Get the link
        a_element = await article.query_selector("a")
        if not a_element:
            continue
        href = await a_element.get_attribute("href")
        if not href:
            continue

        # Check datetime if filtering is enabled
        if filter_timestamp:
            time_element = await article.query_selector("time")
            if time_element:
                datetime_str = await time_element.get_attribute("datetime")
                if datetime_str:
                    article_timestamp = parse_datetime_attr(datetime_str)
                    if article_timestamp < filter_timestamp:
                        continue  # Skip articles before the filter month

        links.append(urljoin(base_url, href))

    return links


async def get_image_links(page, base_url):
    elements = await page.query_selector_all("a")
    img_urls = []
    for a in elements:
        href = await a.get_attribute("href")
        if href:
            full_url = urljoin(base_url, href)
            if is_image_url(full_url):
                img_urls.append(full_url)
    return list(dict.fromkeys(img_urls))  # Remove duplicates while preserving order


async def process_article_page(
    link,
    browser,
    session,
    progress,
    download_sem,
    failed_images,
    stats,
    existing_titles,
    save_dir,
    url_to_path,
):
    for attempt in range(MAX_PAGE_RETRY + 1):
        page = await browser.new_page()
        try:
            await page.route(
                "**/*",
                lambda route: (
                    route.abort()
                    if route.request.resource_type in ["image", "stylesheet", "font"]
                    else route.continue_()
                ),
            )
            try:
                await page.goto(link, wait_until="domcontentloaded", timeout=20000)
            except Exception as e:
                if attempt < MAX_PAGE_RETRY:
                    await asyncio.sleep(2)
                    continue
                else:
                    print(f"\nFailed to open {link} after retries: {e}")
                    return

            # 文章標題
            try:
                await page.wait_for_selector("h1.post__title", timeout=10000)
            except Exception:
                pass

            title_element = await page.query_selector("h1.post__title")
            title = (
                await title_element.inner_text() if title_element else "untitled"
            ).strip()
            title = re.sub(r"[\\/:\*\?\"<>|]", "_", title) or "untitled"

            # 取得文章發布日期 (使用 datetime 屬性)
            timestamp = int(time.time())  # Default to current time
            time_el = await page.query_selector("time")
            if time_el:
                datetime_str = await time_el.get_attribute("datetime")
                if datetime_str:
                    timestamp = parse_datetime_attr(datetime_str)

            if title.startswith("untitled"):
                uid = re.search(r"/post/(\d+)", link)
                uid = uid.group(1) if uid else str(hash(link))
                title = f"{title}_{uid}"

            base_title = title
            suffix = 1
            while title in existing_titles:
                title = f"{base_title}_{suffix}"
                suffix += 1
            existing_titles.add(title)

            figures = await page.query_selector_all("figure")
            if not figures:
                if attempt < MAX_PAGE_RETRY:
                    await asyncio.sleep(1)
                    continue
                else:
                    print(f"\nNo figure found on {link}")
                    return

            img_urls = await get_image_links(page, link)  # Already deduplicated
            progress["total"] += len(img_urls)

            tasks = []
            for idx, img in enumerate(img_urls, start=1):
                ext = os.path.splitext(img)[1].split("?")[0] or ".jpg"
                filename = f"{title}_{idx}{ext}"
                save_path = os.path.join(save_dir, filename)
                url_to_path[img] = save_path
                tasks.append(
                    download_image(
                        session,
                        img,
                        save_dir,
                        progress,
                        download_sem,
                        failed_images,
                        save_path_override=save_path,
                        stats=stats,
                        mtime=timestamp,  # 設定修改時間
                    )
                )
            await asyncio.gather(*tasks)
            break
        finally:
            await page.close()


async def main():
    base_url = input("URL? ")

    # Get filter month from user (format: YY MM)
    month_input = input("Filter month (format: YY MM, default '25 01' for 2025/01, 'n' to disable): ").strip()
    if month_input.lower() == 'n':
        filter_month = None
        print("Month filter disabled")
    elif month_input == '':
        filter_month = (2025, 1)
        print(f"Filtering articles from 2025/01 onwards")
    else:
        try:
            parts = month_input.split()
            if len(parts) != 2:
                raise ValueError("Invalid format")
            year_2digit = int(parts[0])
            month = int(parts[1])
            if month < 1 or month > 12:
                raise ValueError("Invalid month")
            filter_month = (2000 + year_2digit, month)
            print(f"Filtering articles from {filter_month[0]}/{filter_month[1]:02d} onwards")
        except ValueError:
            print("Invalid input, using default 2025/01")
            filter_month = (2025, 1)

    os.makedirs("imgs", exist_ok=True)

    connector = aiohttp.TCPConnector(ssl=False, limit=100, limit_per_host=30)
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(base_url)
            await page.wait_for_load_state("networkidle")

            text = await page.inner_text("body")
            match = re.search(r"Showing\s+\d+\s*-\s*\d+\s+of\s+(\d+)", text)
            total_items = int(match.group(1)) if match else 0

            # 抓作者名
            try:
                await page.wait_for_selector("span[itemprop='name']", timeout=10000)
            except Exception:
                pass
            author_el = await page.query_selector("span[itemprop='name']")
            author_name = await author_el.inner_text() if author_el else "unknown"
            author_name = (
                re.sub(r"[\\/:\*\?\"<>|]", "_", author_name).strip() or "unknown"
            )
            save_dir = os.path.join("imgs", author_name)
            os.makedirs(save_dir, exist_ok=True)
            await page.close()

            print(f"Author: {author_name}")
            print(f"Total articles found: {total_items}")

            page_urls = [base_url] + [
                f"{base_url}?o={offset}" for offset in range(50, total_items, 50)
            ]
            print(f"Total pages: {len(page_urls)}")

            article_links_all = []
            page_semaphore = asyncio.Semaphore(MAX_PAGE_CONCURRENCY)

            async def fetch_article_links(page_url):
                async with page_semaphore:
                    page = await browser.new_page()
                    try:
                        await page.goto(page_url)
                        await page.wait_for_selector("article", timeout=10000)
                        return await get_article_links(page, page_url, filter_month)
                    finally:
                        await page.close()

            spinner_task = asyncio.create_task(spinner("Fetching pages..."))
            results = await asyncio.gather(*[fetch_article_links(u) for u in page_urls])
            spinner_task.cancel()
            await asyncio.sleep(0.1)

            for r in results:
                article_links_all.extend(r)

            print(f"Total article links collected: {len(article_links_all)}")

            progress = {"done": 0, "total": 0}
            stats = {
                "OK_first": 0,
                "OK_retry": 0,
                "SKIP": 0,
                "FAIL_final": 0,
                "ERR_final": 0,
            }
            download_sem = asyncio.Semaphore(MAX_DOWNLOAD_CONCURRENCY)
            failed_images = []
            existing_titles = set()
            url_to_path = {}

            spinner_task = asyncio.create_task(spinner("Downloading images..."))
            for i in range(0, len(article_links_all), BATCH_SIZE):
                batch = article_links_all[i : i + BATCH_SIZE]
                tasks = [
                    process_article_page(
                        link,
                        browser,
                        session,
                        progress,
                        download_sem,
                        failed_images,
                        stats,
                        existing_titles,
                        save_dir,
                        url_to_path,
                    )
                    for link in batch
                ]
                await asyncio.gather(*tasks)
            spinner_task.cancel()
            await asyncio.sleep(0.1)

            # retry failed images
            for attempt in range(MAX_RETRY):
                if not failed_images:
                    break
                current_failed = []

                # Extract mtime from file path if it exists
                def get_mtime_from_path(url):
                    path = url_to_path.get(url)
                    if path and os.path.exists(path):
                        return int(os.path.getmtime(path))
                    return None

                tasks = [
                    download_image(
                        session,
                        url,
                        save_dir,
                        progress,
                        download_sem,
                        current_failed,
                        save_path_override=url_to_path.get(url),
                        stats=stats,
                        is_retry=True,
                        mtime=get_mtime_from_path(url),
                    )
                    for url in failed_images
                ]
                await asyncio.gather(*tasks)
                failed_images = current_failed

            print("\nDownload summary:")
            print("OK_first :", stats["OK_first"])
            print("OK_retry :", stats["OK_retry"])
            print("SKIP     :", stats["SKIP"])
            print("FAIL_fin :", stats["FAIL_final"])
            print("ERR_fin  :", stats["ERR_final"])

            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
