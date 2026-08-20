import requests

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Referer": "https://stpete.granicus.com/",
}

print("=== Follow DownloadFile.php redirect for clip_id=7272 ===")
r = requests.get("https://stpete.granicus.com/DownloadFile.php?view_id=15&clip_id=7272",
                  headers=BROWSER_HEADERS, timeout=30, allow_redirects=True)
print("Final URL:", r.url)
print("Status:", r.status_code, "Content-Type:", r.headers.get("Content-Type"), "Length:", len(r.content))

print()
print("=== Retry old direct URL with browser headers + range ===")
r2 = requests.get("https://archive-video.granicus.com/stpete/stpete_471cef6f-8231-11ee-852f-0050569183fa.mp3",
                   headers={**BROWSER_HEADERS, "Range": "bytes=0-3000000"}, timeout=30)
print("Status:", r2.status_code, "bytes:", len(r2.content), "Content-Type:", r2.headers.get("Content-Type"))
