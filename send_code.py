import json
import urllib.request
import time

BOT = "8925325612:AAFBkmQqBSDm4fc7_kKsQvcw6wnP9QgFU4A"
UID = "748834052"

def send(text, label=""):
    text_safe = text[:4000]
    msg = f"📄 <b>db_adapter.py</b> {label}\n\n<pre>{text_safe}</pre>"
    payload = json.dumps({
        "chat_id": UID,
        "text": msg,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT}/sendMessage",
        data=payload.encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read())
            mid = d.get("result", {}).get("message_id")
            print(f"  ✅ msg {mid}: {len(text)} chars")
            return mid
    except Exception as e:
        print(f"  ❌ {e}")
        return None

with open("/workspace/db_adapter.py") as f:
    content = f.read()

lines = content.split("\n")
half = len(lines) // 2

part1 = "\n".join(lines[:half + 1])
part2 = "\n".join(lines[half + 1:])

print(f"db_adapter.py total: {len(lines)} lines")
print(f"  Part 1: lines 1-{half+1} -> {len(part1)} chars")
print(f"  Part 2: lines {half+2}-{len(lines)} -> {len(part2)} chars")
print()

send(part1, f"(1/2, lines 1-{half+1})")
time.sleep(0.4)
send(part2, f"(2/2, lines {half+2}-{len(lines)})")
