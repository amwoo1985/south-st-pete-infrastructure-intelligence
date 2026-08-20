import os
import requests
from openai import OpenAI

env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
env = {}
with open(env_path) as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k] = v

MP3_URL = "https://archive-video.granicus.com/stpete/stpete_471cef6f-8231-11ee-852f-0050569183fa.mp3"
CLIP_PATH = os.path.join(os.path.dirname(__file__), "granicus_clip_sample.mp3")
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Referer": "https://stpete.granicus.com/",
    "Range": "bytes=0-3000000",
}

print("=== Downloading a short clip via HTTP Range request ===")
resp = requests.get(MP3_URL, headers=BROWSER_HEADERS, timeout=30)
print(f"Status: {resp.status_code}, bytes received: {len(resp.content)}, Content-Type: {resp.headers.get('Content-Type')}")
with open(CLIP_PATH, "wb") as f:
    f.write(resp.content)

print("=== Transcribing via OpenAI ===")
client = OpenAI(api_key=env["OPENAI_API_KEY"])
with open(CLIP_PATH, "rb") as f:
    transcript = client.audio.transcriptions.create(model="whisper-1", file=f)

print(f"PASS: transcript received, length={len(transcript.text)} chars")
print("--- excerpt (first 400 chars) ---")
print(transcript.text[:400])

os.remove(CLIP_PATH)
print("(clip file removed)")
