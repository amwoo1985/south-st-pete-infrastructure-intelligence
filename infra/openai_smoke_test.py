import json
import urllib.request
import urllib.error
import os

env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
env = {}
with open(env_path) as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k] = v

api_key = env.get("OPENAI_API_KEY")
if not api_key:
    print("FAIL: OPENAI_API_KEY not found in .env")
    raise SystemExit(1)

headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

def call(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"HTTP {e.code} error (key redacted from output): {body[:300]}")
        raise SystemExit(1)

print("=== Embedding call ===")
emb = call("https://api.openai.com/v1/embeddings", {
    "model": "text-embedding-3-small",
    "input": "South St. Petersburg Community Benefits Agreement smoke test"
})
vec = emb["data"][0]["embedding"]
print(f"PASS: embedding returned, dimensions={len(vec)}, model={emb.get('model')}")

print("=== Chat completion call ===")
chat = call("https://api.openai.com/v1/chat/completions", {
    "model": "gpt-4o-mini",
    "messages": [{"role": "user", "content": "Reply with exactly: SMOKE_TEST_OK"}],
    "max_tokens": 10
})
reply = chat["choices"][0]["message"]["content"].strip()
print(f"PASS: chat completion returned: {reply!r} (model={chat.get('model')})")
