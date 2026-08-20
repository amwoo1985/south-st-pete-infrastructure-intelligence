import requests
import re

print("=== 403 response body from the old URL ===")
r = requests.get("https://archive-video.granicus.com/stpete/stpete_471cef6f-8231-11ee-852f-0050569183fa.mp3",
                  headers={"Range": "bytes=0-3000000"}, timeout=30)
print(r.status_code, r.text[:500])

print()
print("=== Fetching RSS feed for a recent meeting ===")
rss = requests.get("https://stpete.granicus.com/ViewPublisherRSS.php?view_id=15&mode=podcast", timeout=30)
print("RSS status:", rss.status_code)
# find first enclosure url
m = re.search(r'<enclosure url=[\'"]([^\'"]+)[\'"]', rss.text)
print("First enclosure URL:", m.group(1) if m else "NOT FOUND")
title_m = re.search(r'<title>([^<]+)</title>', rss.text[rss.text.find('<item>'):])
print("First item title:", title_m.group(1) if title_m else "?")
