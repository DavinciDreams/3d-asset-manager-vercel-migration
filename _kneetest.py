from playwright.sync_api import sync_playwright
import time
MID="samba-dancing-local01"
VRMA_VIEW="/api/view/f7ea5a2c"  # will resolve full id below
with sync_playwright() as p:
    b=p.chromium.launch(headless=True,args=["--enable-unsafe-swiftshader","--use-gl=swiftshader"]); pg=b.new_page()
    pg.set_viewport_size({"width":900,"height":900})
    errs=[]; pg.on("console",lambda m: errs.append(m.text[:160]) if m.type=="error" else None)
    pg.goto("http://localhost:8000/auth/login",wait_until="domcontentloaded")
    pg.fill("input[type=text]","swagtester"); pg.fill("input[type=password]","rigtest123")
    pg.click("button[type=submit], input[type=submit]"); pg.wait_for_load_state("networkidle")
    pg.goto(f"http://localhost:8000/model/{MID}",wait_until="networkidle"); time.sleep(2)
    # switch to VRM
    pg.evaluate("()=>switchVariant('vrm')")
    for _ in range(40):
        time.sleep(0.5)
        if pg.evaluate("()=>!!window._vrmController && !document.getElementById('vrm-loading-%s')"%MID): break
    # find the full vrma id and apply via the picker
    vid = pg.evaluate("""() => {
      const sel=document.getElementById('vrma-select');
      if(!sel) return null;
      for(const o of sel.options){ if((o.textContent||'').toLowerCase().includes('you dance')) return o.value; }
      return sel.options.length>1 ? sel.options[1].value : null;
    }""")
    print("vrma url to apply:", vid)
    if vid:
        ok = pg.evaluate("(u)=>window._vrmController.applyAnimation(u)", vid)
        print("applyAnimation returned:", ok)
        time.sleep(2.5)  # let it animate a bit
    pg.screenshot(path="_samba_vrm.png")
    print("screenshot saved; errors:", [e for e in errs if 'favicon' not in e][:5])
    b.close()
