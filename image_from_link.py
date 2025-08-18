import asyncio
from playwright.async_api import async_playwright
from urllib.parse import urljoin
import aiohttp
import aiofiles
import os
import re
import itertools

IMG_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
MAX_PAGE_CONCURRENCY = 3
MAX_DOWNLOAD_CONCURRENCY = 20
MAX_RETRY = 2
MAX_PAGE_RETRY = 2


def is_image_url(url):
    return url.lower().endswith(IMG_EXTENSIONS)


# ---------------- Spinner ----------------
async def spinner(msg="Processing"):
    try:
        while True:
            for char in r"-\|/":
                print(f"\r{msg} {char}", end="", flush=True)
                await asyncio.sleep(0.1)
    except asyncio.CancelledError:
        print("\r" + " " * (len(msg) + 2) + "\r", end="", flush=True)


# ---------------- Download ----------------
async def download_image(
    session,
    url,
    save_dir,
    progress,
    sem,
    failed_images,
    save_path_override=None,
    stats=None,
):
    async with sem:
        try:
            if save_path_override:
                save_path = save_path_override
            else:
                filename = url.split("/")[-1].split("?")[0]
                save_path = os.path.join(save_dir, filename)

            if os.path.exists(save_path):
                progress["done"] += 1
                print("\r", end="")
                print(
                    f"[{progress['done']}/{progress['total']}] SKIP {os.path.basename(save_path)}"
                )
                if stats is not None:
                    stats["SKIP"] += 1
                return True

            async with session.get(url) as resp:
                if resp.status == 200:
                    async with aiofiles.open(save_path, "wb") as f:
                        await f.write(await resp.read())
                    progress["done"] += 1
                    print("\r", end="")
                    print(
                        f"[{progress['done']}/{progress['total']}] OK {os.path.basename(save_path)}"
                    )
                    if stats is not None:
                        stats["OK"] += 1
                    return True
                else:
                    progress["done"] += 1
                    print("\r", end="")
                    print(f"[{progress['done']}/{progress['total']}] FAIL {url}")
                    failed_images.append(url)
                    if stats is not None:
                        stats["FAIL"] += 1
                    return False
        except Exception as e:
            progress["done"] += 1
            print("\r", end="")
            print(f"[{progress['done']}/{progress['total']}] ERR {url} {e}")
            failed_images.append(url)
            if stats is not None:
                stats["ERR"] += 1
            return False


# ---------------- Playwright ----------------
async def get_article_links(page, base_url):
    elements = await page.query_selector_all("article a")
    links = []
    for a in elements:
        href = await a.get_attribute("href")
        if href:
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
    return img_urls


async def process_article_page(
    link, browser, session, progress, download_sem, failed_images, stats
):
    for attempt in range(MAX_PAGE_RETRY + 1):
        page = await browser.new_page()
        try:
            try:
                await page.goto(link, wait_until="domcontentloaded", timeout=20000)
            except Exception as e:
                if attempt < MAX_PAGE_RETRY:
                    await asyncio.sleep(1)
                    continue
                else:
                    print(f"Failed to open {link} after retries: {e}")
                    return

            # 取文章標題
            title_element = await page.query_selector("h1.post__title")
            if title_element:
                title = await title_element.inner_text()
                title = re.sub(r"[\\/:\*\?\"<>|]", "_", title)
                if title.strip() == "":
                    title = "untitled"
            else:
                title = "untitled"

            # 對 untitled 或空標題加文章 ID 生成唯一名稱
            if title.startswith("untitled"):
                uid = re.search(r"/post/(\d+)", link)
                uid = uid.group(1) if uid else str(hash(link))
                title = f"{title}_{uid}"

            # 抓所有圖片
            figures = await page.query_selector_all("figure")
            if not figures:
                if attempt < MAX_PAGE_RETRY:
                    await asyncio.sleep(1)
                    continue
                else:
                    print(
                        f"No figure found on {link} after {MAX_PAGE_RETRY+1} attempts"
                    )
                    return

            img_urls = await get_image_links(page, link)
            progress["total"] += len(img_urls)

            # 下載圖片，依序號命名
            tasks = []
            for idx, img in enumerate(img_urls, start=1):
                ext = os.path.splitext(img)[1].split("?")[0] or ".jpg"
                filename = f"{title}_{idx}{ext}"
                save_path = os.path.join("imgs", filename)
                tasks.append(
                    download_image(
                        session,
                        img,
                        "imgs",
                        progress,
                        download_sem,
                        failed_images,
                        save_path_override=save_path,
                        stats=stats,
                    )
                )
            await asyncio.gather(*tasks)
            break
        finally:
            await page.close()


# ---------------- Main ----------------
async def main():
    base_url = input("URL? ")
    os.makedirs("imgs", exist_ok=True)

    connector = aiohttp.TCPConnector(ssl=False)
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)

            spinner_task = asyncio.create_task(spinner("Fetching pages..."))

            page = await browser.new_page()
            await page.goto(base_url)
            await page.wait_for_load_state("networkidle")
            text = await page.inner_text("body")
            match = re.search(r"Showing\s+\d+\s*-\s*\d+\s+of\s+(\d+)", text)
            total_items = int(match.group(1)) if match else 0
            await page.close()

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
                        links = await get_article_links(page, page_url)
                        return links
                    finally:
                        await page.close()

            results = await asyncio.gather(*[fetch_article_links(u) for u in page_urls])
            for r in results:
                article_links_all.extend(r)

            spinner_task.cancel()
            await asyncio.sleep(0.1)

            print(f"Total article links collected: {len(article_links_all)}")

            progress = {"done": 0, "total": 0}
            stats = {"OK": 0, "SKIP": 0, "FAIL": 0, "ERR": 0}
            download_sem = asyncio.Semaphore(MAX_DOWNLOAD_CONCURRENCY)
            failed_images = []

            for attempt in range(MAX_RETRY + 1):
                spinner_task = asyncio.create_task(
                    spinner(f"Downloading images (round {attempt})...")
                )
                current_failed = []
                tasks = (
                    [
                        process_article_page(
                            link,
                            browser,
                            session,
                            progress,
                            download_sem,
                            current_failed,
                            stats,
                        )
                        for link in article_links_all
                    ]
                    if attempt == 0
                    else [
                        download_image(
                            session,
                            img,
                            "imgs",
                            progress,
                            download_sem,
                            current_failed,
                            stats=stats,
                        )
                        for img in failed_images
                    ]
                )
                await asyncio.gather(*tasks)
                spinner_task.cancel()
                await asyncio.sleep(0.1)
                failed_images = current_failed
                if not failed_images:
                    break

            # 最後統計
            print("\nDownload summary:")
            print(f"OK   : {stats['OK']}")
            print(f"SKIP : {stats['SKIP']}")
            print(f"FAIL : {stats['FAIL']}")
            print(f"ERR  : {stats['ERR']}")

            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
