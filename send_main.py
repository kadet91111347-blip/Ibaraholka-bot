"""
Шлёт main.py и miniapp/index.html в чат по частям с заголовками.
Разбивает по строкам так, чтобы каждый чанк был ≤ 3900 chars (с запасом на заголовок).
"""
import json
import urllib.request
import time
import os

BOT = "8925325612:AAFBkmQqBSDm4fc7_kKsQvcw6wnP9QgFU4A"
UID = 748834052
LIMIT = 3900  # chars per message (Telegram 4096 - header overhead)

def send(text: str) -> bool:
    payload = json.dumps({
        "chat_id": UID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT}/sendMessage",
        data=payload.encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read())
            if d.get("ok"):
                mid = d["result"]["message_id"]
                return mid
            print(f"  ❌ {d}")
            return None
    except Exception as e:
        print(f"  ❌ {e}")
        return None

def split_by_lines(path: str, max_chars: int):
    """Yield chunks that fit within max_chars without breaking lines."""
    with open(path) as f:
        lines = f.readlines()
    chunks = []
    cur = []
    cur_len = 0
    for line in lines:
        if cur_len + len(line) > max_chars and cur:
            chunks.append("".join(cur))
            cur = [line]
            cur_len = len(line)
        else:
            cur.append(line)
            cur_len += len(line)
    if cur:
        chunks.append("".join(cur))
    return chunks

def send_file(path: str, label: str, lang: str = "python"):
    print(f"\n=== {label} ===")
    chunks = split_by_lines(path, LIMIT - 200)  # оставим ~200 на заголовок
    total = len(chunks)
    print(f"  {total} messages")
    line_counts = []
    offset = 0
    for i, body in enumerate(chunks, 1):
        line_count = body.count("\n")
        line_counts.append((offset + 1, offset + line_count))
        offset += line_count

    successes = 0
    for i, (body, (l_from, l_to)) in enumerate(zip(chunks, line_counts), 1):
        if i == 1:
            head = f"📦 <b>{label}</b> ({total} частей)\n\n"
        else:
            head = f"<b>{label} · {i}/{total}</b> · стр.{l_from}-{l_to}\n\n"
        # Экранируем HTML в теле
        body_safe = (body.replace("&", "&amp;")
                          .replace("<", "&lt;")
                          .replace(">", "&gt;"))
        msg = head + f"<pre>{body_safe}</pre>"
        mid = send(msg)
        if mid:
            successes += 1
            if i % 5 == 0 or i == total:
                print(f"  {i}/{total} sent (last msg_id={mid})")
        else:
            print(f"  ⚠️  chunk {i}/{total} failed")
        # Respect Telegram rate limits: 30 msg/sec — we send ~20 msg/sec
        time.sleep(0.05)

    print(f"  ✅ {successes}/{total} sent")
    return successes, total

if __name__ == "__main__":
    send_file("/workspace/main.py", "main.py", lang="python")
    time.sleep(1)
    send_file("/workspace/miniapp/index.html", "miniapp/index.html", lang="html")
    time.sleep(1)
    # Final marker
    final = (
        "✅ <b>Готово!</b>\n\n"
        "Скопировал всё. Если что-то разорвалось посередине строки — "
        "проверь, что склеил все части по порядку (1/N → N/N)."
    )
    send(final)
