#!/usr/bin/env python3
"""v62: убираем apiPost('/payments/yukassa/create') — он перезаписывает правильный
tbank URL на битый (без /aHI4Y75190 → Тиньков 404)."""
import pathlib, sys

SRC = pathlib.Path("/tmp/v61-clean")

OLD = """        // Перед openLink зовём бэк /payments/yukassa/create чтобы получить актуальный url
        let url='https://www.tbank.ru/rm/r_TGugYbYVEb.mLmrPUwlTy/aHI4Y75190?amount='+(amount*100)+'&successURL=https://t.me/Ibaraholka_bot';
        try{
          const r=await apiPost('/payments/yukassa/create',{listing_id:listingId,tier,user_id:window.tg?.initDataUnsafe?.user?.id||0});
          if(r&&(r.tinkoff_url||r.confirmation_url))url=r.tinkoff_url||r.confirmation_url;
        }catch(_e){}"""

NEW = """        // v62: НЕ зовём /payments/yukassa/create — он возвращает битый URL без /aHI4Y75190 (Тиньков → 404).
        // Используем напрямую захардкоженный правильный URL.
        const url='https://www.tbank.ru/rm/r_TGugYbYVEb.mLmrPUwlTy/aHI4Y75190?amount='+(amount*100)+'&successURL=https://t.me/Ibaraholka_bot';"""

OLD_V = "var v='v61'"
NEW_V = "var v='v62'"

ok = True
for d in SRC.iterdir():
    if not d.is_dir():
        continue
    p = d / "index.html"
    if not p.exists():
        continue
    s = p.read_text()
    if OLD not in s:
        print(f"  ERR {d.name}: OLD not found")
        ok = False
        continue
    s = s.replace(OLD, NEW).replace(OLD_V, NEW_V)
    p.write_text(s)
    print(f"  OK {d.name}: {len(s)} bytes")

sys.exit(0 if ok else 1)
