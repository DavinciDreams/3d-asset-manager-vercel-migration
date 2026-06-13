from playwright.sync_api import sync_playwright
import time
MID="0c1c61ef-0ac7-4fbd-b8e4-af0c40465bd4"  # emmy
with sync_playwright() as p:
    b=p.chromium.launch(headless=True,args=["--enable-unsafe-swiftshader","--use-gl=swiftshader"]); pg=b.new_page()
    pg.set_viewport_size({"width":900,"height":900})
    errs=[]; pg.on("console",lambda m: errs.append(m.text[:160]) if m.type=="error" else None)
    pg.goto("http://localhost:8000/auth/login",wait_until="domcontentloaded")
    pg.fill("input[type=text]","swagtester"); pg.fill("input[type=password]","rigtest123")
    pg.click("button[type=submit], input[type=submit]"); pg.wait_for_load_state("networkidle")
    pg.goto(f"http://localhost:8000/model/{MID}",wait_until="networkidle"); time.sleep(2)
    pg.evaluate("()=>switchVariant('vrm')")
    for _ in range(40):
        time.sleep(0.5)
        if pg.evaluate("()=>!!window._vrmController && !document.getElementById('vrm-loading-%s')"%MID): break
    vid = pg.evaluate("""() => {
      const sel=document.getElementById('vrma-select');
      if(!sel) return null;
      for(const o of sel.options){ if((o.textContent||'').toLowerCase().includes('you dance')) return o.value; }
      return sel.options.length>1 ? sel.options[1].value : null;
    }""")
    print("dance url:", vid)
    if vid:
        # zoom camera onto the avatar via controller, then apply + sample bone world rotations
        ok = pg.evaluate("(u)=>window._vrmController.applyAnimation(u)", vid)
        print("applied:", ok)
        time.sleep(1.5)
        # read the knee (lowerLeg) bone local rotation at this animation frame
        info = pg.evaluate("""() => {
          const vrm = window._vrmController.vrm;
          if(!vrm||!vrm.humanoid) return {err:'no humanoid'};
          const getBone = (n) => vrm.humanoid.getNormalizedBoneNode ? vrm.humanoid.getNormalizedBoneNode(n) : (vrm.humanoid.getBoneNode ? vrm.humanoid.getBoneNode(n) : null);
          const out={};
          for(const bn of ['leftUpperLeg','leftLowerLeg','rightLowerLeg']){
            const node=getBone(bn);
            if(node){ const e=node.rotation; out[bn]={x:+e.x.toFixed(2),y:+e.y.toFixed(2),z:+e.z.toFixed(2)}; }
          }
          return out;
        }""")
        print("knee bone rotations during dance:", info)
    pg.screenshot(path="_emmy_vrm.png")
    b.close()
