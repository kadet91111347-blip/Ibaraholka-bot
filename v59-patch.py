import sys

# Ищем маркер: функцию openSell в JS и в конце load(); 
# Также ищем fetch(url...) — первый fetch в sellSubmit это /listings POST
WARMUP_JS = """\n\n// v59: keep-alive ping every 14 min so Render free instance doesn't sleep
function ibaraholkaWarmup(){try{fetch(API+'/health',{method:'GET',cache:'no-store'}).catch(()=>{});}catch(e){}}
if(window.ibaraholkaWarmupTimer)clearInterval(window.ibaraholkaWarmupTimer);
window.ibaraholkaWarmupTimer=setInterval(ibaraholkaWarmup,14*60*1000);
setTimeout(ibaraholkaWarmup,30000);\n"""

for fname in ['Ibaraholka-bot.html', 'apple-mini-app.html', 'baraholka-iphone.html', 'ibaraholka-apple.html']:
    with open(fname, 'r', encoding='utf-8') as f:
        s = f.read()
    
    orig = s
    # 1. version
    s = s.replace("var v='v54'", "var v='v59'")
    s = s.replace("var v='v58'", "var v='v59'")
    
    # 2. fetch timeout — заменяем ВСЕ `await fetch(` на `await Promise.race([fetch(...), timeout(60000)])`
    # Но Promise.race требует объявления timeout(), добавим helper
    if 'const _fetchTimeout=60000' not in s:
        s = s.replace(
            'buildFilters();',
            'buildFilters();\nconst _fetchTimeout=60000;function _toPR(p,t){return Promise.race([p,new Promise((_,r)=>setTimeout(()=>r(new Error(\'network-timeout\')),t))]);}\n'
        )
        # Заменяем все fetch на _toPR-fetch (нужно добавить лишнюю ')' потом)
        s = s.replace('await fetch(', 'await _toPR(fetch(', 1)
        # И заменяем закрывающую ')' fetch на '))' _toPR(fetch(...)) 
        # Pattern: 'fetch(API+p,{...});' → 'fetch(API+p,{...}))'
        # Лучше явная замена по конкретной сигнатуре
        s = s.replace("fetch(API+p,{cache:'no-store',headers:auth()})", "fetch(API+p,{cache:'no-store',headers:auth()})", 1)
        # Ставлю лишнюю ')' перед ;
        s = s.replace("auth()});if(!r.ok)", "auth()}));if(!r.ok)", 1)
    
    # 3. warmup в самом конце
    if 'ibaraholkaWarmupTimer' not in s:
        if 'load();\n</script>' in s:
            s = s.replace('load();\n</script>', 'load();\n' + WARMUP_JS + '</script>')
        elif 'load();</script>' in s:
            s = s.replace('load();</script>', 'load();' + WARMUP_JS + '</script>')
    
    with open(fname, 'w', encoding='utf-8') as f:
        f.write(s)
    
    changes = "OK" if s != orig else "NO CHANGES"
    sz = len(s)
    print(f"{fname}: {changes} ({sz} bytes)")
