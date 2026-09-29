"""Offline DOM + API integration; no browser navigation, microphone or vendor calls."""
import base64,sys,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright
from studio.runtime import create_app
ROOT=Path(__file__).resolve().parents[1]
def main():
 with tempfile.TemporaryDirectory() as tmp:
  app=create_app(tmp);user=app.state.store.create_user('QA owner','admin');client=TestClient(app)
  def bridge(item):
   r=client.request(item['method'],item['path'],headers=item['headers'],content=item.get('body'))
   return dict(status=r.status_code,body=base64.b64encode(r.content).decode(),headers=dict(r.headers))
  with sync_playwright() as p:
   browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])
   page=browser.new_page(viewport={'width':1280,'height':960});errors=[]
   page.on('pageerror',lambda e:errors.append(str(e)));page.on('dialog',lambda d:d.accept())
   page.expose_function('__apiBridge',bridge)
   static=ROOT/'studio/static'
   html=(static/'guided.html').read_text().replace('<link rel="stylesheet" href="/static/guided.css">','<style>'+(static/'guided.css').read_text()+'</style>').replace('<script type="module" src="/static/guided.js"></script>','')
   page.set_content(html)
   page.add_script_tag(content='''const mem=new Map();Object.defineProperty(window,'sessionStorage',{value:{getItem:k=>mem.get(k)||null,setItem:(k,v)=>mem.set(k,v),removeItem:k=>mem.delete(k)}});if(!crypto.randomUUID)crypto.randomUUID=()=>Array.from(crypto.getRandomValues(new Uint8Array(16)),x=>x.toString(16).padStart(2,'0')).join('');window.fetch=async(path,options={})=>{const r=await window.__apiBridge({path:String(path),method:options.method||'GET',headers:Object.fromEntries(new Headers(options.headers||{})),body:options.body||null});return new Response(Uint8Array.from(atob(r.body),c=>c.charCodeAt(0)),{status:r.status,headers:r.headers})};''')
   recorder=(static/'recorder.js').read_text().replace('export function','function').replace('export class','class')
   code=(static/'guided.js').read_text().replace("import {Recorder} from './recorder.js';",'')
   page.add_script_tag(content='(()=>{'+recorder+'\n'+code+'})();')
   page.locator('#token').fill(user['token']);page.locator('#login button').click()
   page.locator('#name').fill('QA test speaker');page.locator('#style').fill('Brief natural questions.');page.locator('#profileForm button').click()
   page.locator('#collect').check();page.locator('#self').check();page.locator('#behavior').check();page.locator('#voice').check();page.locator('#consentForm button').click()
   page.locator('#start').click()
   page.locator('#answer').fill('شو الأهم عندك، السكن ولا الاستثمار؟');page.locator('#next').click()
   page.locator('#answer').fill('The primary objective determines the next question.');page.locator('#next').click()
   page.locator('#answer').fill('A near-term move makes immediate housing more important.');page.locator('#next').click()
   page.locator('#action').select_option('clarify');page.locator('#verified').check();page.locator('#next').click()
   page.locator('[data-review$=":approved"]').click();page.wait_for_function("document.querySelector('[data-review$=\":approved\"]').disabled")
   page.locator('#gotest').click();page.locator('#build').click();page.locator('#pack').wait_for();page.locator('summary').click();page.locator('#showprompt').click()
   page.wait_for_function("document.querySelector('#prompt').textContent.includes('شو الأهم')")
   assert page.locator('#compare').is_disabled() and page.locator('#publish').is_disabled()
   assert len(app.state.store.all('SELECT * FROM teaching_packs'))==1
   assert not app.state.store.all('SELECT * FROM teaching_publications')
   page.locator('[data-nav=home]').click();page.locator('#start').wait_for()
   page.screenshot(path='/mnt/data/raneen-guided-desktop.png',full_page=True)
   page.set_viewport_size({'width':390,'height':844});page.screenshot(path='/mnt/data/raneen-guided-mobile.png',full_page=True)
   assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
   assert not errors,errors
   print('Offline DOM/API onboarding, typed teaching, human review, behavior pack, missing-key gates and mobile overflow checks passed. Real microphone/navigation/provider tests were not performed.')
   browser.close()
if __name__=='__main__':main()
