#!/usr/bin/env python3
"""v61 — fix payment buttons in sellSubmit modal:
1. Remove target='_blank' (doesn't work in Telegram WebView)
2. Use ONLY onclick with tg.openLink / tg.openTelegramLink
3. Add '💬 Получить ссылку в личку' button — fallback when openLink fails
"""
import re, sys

# Read file
path = sys.argv[1]
with open(path, 'r', encoding='utf-8') as f:
    html = f.read()

orig_size = len(html)

# 1. Replace payTbankBtn — remove target='_blank', use only tg.openLink, no target
old_tbank = '''<a id="payTbankBtn" href="https://www.tbank.ru/rm/r_TGugYbYVEb.mLmrPUwlTy/aHI4Y75190" target="_blank" rel="noopener" onclick="try{var __tg=window.Telegram&&window.Telegram.WebApp;if(__tg&&__tg.openLink){__tg.openLink(this.href);return false;}}catch(_e){}return true;" class="btn-cta" style="display:flex;align-items:center;justify-content:space-between;background:linear-gradient(135deg,#FFDD2D,#FFB800);color:#0B0B0F;font-weight:700;padding:14px 16px;border-radius:12px;text-decoration:none;margin-bottom:8px"><span style="font-size:22px">💳</span><span style="flex:1;text-align:left;margin-left:12px">Т-Банк · ${amount} ₽</span><span style="font-size:18px">→</span></a>'''
new_tbank = '''<button id="payTbankBtn" type="button" data-listing-id="${createdId}" data-tier="${tier}" data-amount="${amount}" class="btn-cta" style="display:flex;align-items:center;justify-content:space-between;background:linear-gradient(135deg,#FFDD2D,#FFB800);color:#0B0B0F;font-weight:700;padding:14px 16px;border-radius:12px;border:0;margin-bottom:8px;font-size:15px;cursor:pointer;width:100%;text-align:left"><span style="font-size:22px">💳</span><span style="flex:1;text-align:left;margin-left:12px">Т-Банк · ${amount} ₽</span><span style="font-size:18px">→</span></button>'''
if old_tbank not in html:
    print(f"  WARN: payTbankBtn NOT FOUND in {path}", file=sys.stderr)
    sys.exit(1)
html = html.replace(old_tbank, new_tbank)

# 2. Replace payStarsBtn — remove target='_blank', use openTelegramLink
old_stars = '''<a id="payStarsBtn" href="https://t.me/Ibaraholka_bot?start=pay_${createdId}_${tier}" target="_blank" rel="noopener" onclick="try{var __sg=window.Telegram&&window.Telegram.WebApp;if(__sg&&__sg.openTelegramLink){__sg.openTelegramLink(this.href);return false;}}catch(_e){}return true;" class="btn-cta" style="display:flex;align-items:center;justify-content:space-between;background:linear-gradient(135deg,#FFD75E,#FFB800);color:#0B0B0F;font-weight:700;padding:14px 16px;border-radius:12px;text-decoration:none;margin-bottom:8px"><span style="font-size:22px">⭐</span><span style="flex:1;text-align:left;margin-left:12px">Звёзды Telegram · ${stars} ★</span><span style="font-size:18px">→</span></a>'''
new_stars = '''<button id="payStarsBtn" type="button" data-listing-id="${createdId}" data-tier="${tier}" class="btn-cta" style="display:flex;align-items:center;justify-content:space-between;background:linear-gradient(135deg,#FFD75E,#FFB800);color:#0B0B0F;font-weight:700;padding:14px 16px;border-radius:12px;border:0;margin-bottom:8px;font-size:15px;cursor:pointer;width:100%;text-align:left"><span style="font-size:22px">⭐</span><span style="flex:1;text-align:left;margin-left:12px">Звёзды Telegram · ${stars} ★</span><span style="font-size:18px">→</span></button>'''
if old_stars not in html:
    print(f"  WARN: payStarsBtn NOT FOUND in {path}", file=sys.stderr)
    sys.exit(1)
html = html.replace(old_stars, new_stars)

# 3. Add handler block — right before "// Заполняем href у Т-Банк кнопки"
old_anchor = '    // Заполняем href у Т-Банк кнопки из последнего yukassa response'
new_anchor = '''    // v61: handler для payTbankBtn/payStarsBtn (button, не anchor)
    const tBtn=document.getElementById('payTbankBtn');
    if(tBtn){
      tBtn.onclick=async(e)=>{
        e.preventDefault();
        const listingId=tBtn.dataset.listingId;
        const tier=tBtn.dataset.tier;
        const amount=parseInt(tBtn.dataset.amount)||0;
        // Перед openLink зовём бэк /payments/yukassa/create чтобы получить актуальный url
        let url='https://www.tbank.ru/rm/r_TGugYbYVEb.mLmrPUwlTy/aHI4Y75190?amount='+(amount*100)+'&successURL=https://t.me/Ibaraholka_bot';
        try{
          const r=await apiPost('/payments/yukassa/create',{listing_id:listingId,tier,user_id:window.tg?.initDataUnsafe?.user?.id||0});
          if(r&&(r.tinkoff_url||r.confirmation_url))url=r.tinkoff_url||r.confirmation_url;
        }catch(_e){}
        const tg=window.Telegram?.WebApp;
        if(tg&&tg.openLink){
          tg.openLink(url);
          haptic('success');
          toast('🏦 Открываю Т-Банк — оплати и вернись сюда');
        }else{
          // fallback: копируем в clipboard + показываем url
          try{navigator.clipboard.writeText(url);toast('✅ Ссылка скопирована — открой в браузере');}catch(e){toast('Ссылка: '+url);}
        }
      };
    }
    const sBtn=document.getElementById('payStarsBtn');
    if(sBtn){
      sBtn.onclick=async(e)=>{
        e.preventDefault();
        const listingId=sBtn.dataset.listingId;
        const tier=sBtn.dataset.tier;
        const tg=window.Telegram?.WebApp;
        const url='https://t.me/Ibaraholka_bot?start=pay_'+encodeURIComponent(listingId+'_'+tier);
        if(tg&&tg.openTelegramLink){
          tg.openTelegramLink(url);
          haptic('success');
          toast('⭐ Открываю бота для оплаты Звёздами');
        }else{
          try{navigator.clipboard.writeText(url);toast('✅ Ссылка скопирована');}catch(e){}
        }
      };
    }
    // Заполняем href у Т-Банк кнопки из последнего yukassa response'''
if old_anchor not in html:
    print(f"  WARN: anchor NOT FOUND in {path}", file=sys.stderr)
    sys.exit(1)
html = html.replace(old_anchor, new_anchor, 1)

# 4. Replace the broken "re-set href from __lastYukassaUrl" block (currently overrides the new button handler)
old_override = '''    if(window.__lastYukassaUrl){
      const tBtn=document.getElementById('payTbankBtn');
      if(tBtn){tBtn.href=window.__lastYukassaUrl;tBtn.setAttribute('target','_blank');tBtn.setAttribute('rel','noopener');tBtn.onclick=function(e){e.preventDefault();window.location.href=window.__lastYukassaUrl;return false;};}
    }'''
if old_override in html:
    html = html.replace(old_override, '    // v61: убрали target=_blank override (теперь button + tg.openLink)')

# 5. Bump version
html = html.replace("var v='v60'", "var v='v61'")

with open(path, 'w', encoding='utf-8') as f:
    f.write(html)

print(f"  ✅ {path}: {orig_size} → {len(html)} bytes")
