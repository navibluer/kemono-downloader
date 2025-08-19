# Image Downloader from Commer/Kemono Articles

A Python script to download all images from Commer or Kemono article pages, organizing them into folders per artist. **Note:** This script is intended for Commer/Kemono only, not Fantia.


---

## Features

- Fetch all articles from a given URL.
- Extract images and download them with unique filenames.
- Retry failed downloads up to 2 times.
- Organize images in a folder named from `span[itemprop="name"]`.
- Track statistics: first downloads, retries, skips, failures.

---

## Requirements

- Python 3.10+
- Playwright
- aiohttp
- aiofiles

Install dependencies:

```bash
pip install playwright aiohttp aiofiles

## Usage

Run the script and input the artist’s first page URL:

```bash
python image_from_link.py

- The URL must point to the first page of the artist’s content.
- The script will create a folder imgs/<user> based on the span[itemprop="name"] in the page.
- Images are saved as <article_title>_<index>.<ext>.
- Handles retries for failed downloads.
- Tracks stats: first download OK, retry OK, skipped, fail, error.

Notes
- Article titles are taken from h1.post__title. Duplicate titles are made unique automatically.
- Retry attempts only download previously failed images.
- Maximum concurrent page fetch: 3
- Maximum concurrent image downloads: 10
- The script supports common image extensions: .jpg, .jpeg, .png, .gif, .webp, .bmp.