const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ROOT = path.resolve(__dirname, '..');
const DEFAULT_URL = process.env.DEFAULT_URL || 'http://127.0.0.1:4184';
const FIXTURE_URL = process.env.FIXTURE_URL || 'http://127.0.0.1:4183';
(async () => {
  const browser = await chromium.launch({channel:'msedge',headless:true});
  fs.mkdirSync(path.join(ROOT,'test-results'),{recursive:true});
  try {
    for (const [name,width,height] of [['desktop',1440,1000],['mobile',390,844]]) {
      const context = await browser.newContext({viewport:{width,height}}); const page = await context.newPage();
      const errors=[]; page.on('pageerror',e => errors.push(e.message));
      let requests = 0; page.on('request',r => {if(r.url().includes('/api/')) requests++;});
      await page.goto(DEFAULT_URL); await page.getByRole('heading',{name:'Ваши проекты'}).waitFor();
      await page.screenshot({path:path.join(ROOT,'test-results',name+'-projects.png'),fullPage:true});
      await page.getByLabel('Новый проект').fill('Проверить варианты бизнеса'); await page.getByRole('button',{name:/Создать проект/}).click();
      await page.getByLabel('Задача, факты и ссылки').fill('Несекретная задача: сравнить варианты и проверить источники.');
      await page.getByRole('button',{name:'Подключить проект к сервису'}).click();
      await page.getByRole('alert').filter({hasText:'Сервис пока не подключён'}).waitFor(); assert.equal(requests,0);
      await page.screenshot({path:path.join(ROOT,'test-results',name+'-workspace.png'),fullPage:true});
      await page.getByRole('button',{name:'← Все проекты'}).click(); await page.getByRole('button',{name:/Проверить варианты бизнеса/}).click();
      await page.reload(); await page.getByRole('button',{name:/Проверить варианты бизнеса/}).click();
      assert.equal(await page.getByLabel('Задача, факты и ссылки').inputValue(),'Несекретная задача: сравнить варианты и проверить источники.');
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
      assert.deepEqual(errors,[]); await context.close(); console.log('PASS '+name+': default unavailable, create/context/reopen/reload, no API request, no horizontal overflow');
    }
    const ctx = await browser.newContext({viewport:{width:1440,height:1000}}); const page = await ctx.newPage(); const metrics=[]; const saveIds=[]; let saveAttempts=0;
    page.on('requestfailed',r=>console.log('Fixture request failed',r.url(),r.failure())); page.on('console',m=>{if(m.type()==='error')console.log('Fixture browser error',m.text());});
    await page.exposeFunction('captureMetric',e=>metrics.push(e));
    await page.addInitScript(()=>window.addEventListener('coreteams:metric',e=>window.captureMetric(e.detail)));
    await page.route('**/api/**',async route=>{
      const req=route.request(); const url=new URL(req.url()); const suffix=url.pathname.slice(4); console.log('Fixture route',suffix); const json=v=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(v)});
      if(suffix==='/projects') return json({id:'server-project'});
      if(suffix==='/projects/server-project/runs') return json({id:'run-1',status:'running'});
      if(suffix==='/projects/server-project/runs/run-1') return json({id:'run-1',status:'clarification',question:{id:'question-1',text:'Какие ограничения важны?'}});
      if(suffix==='/projects/server-project/runs/run-1/answers') {assert.equal(req.postDataJSON().questionId,'question-1');return json({id:'run-1',status:'completed',result:{id:'result-1',title:'Результат тестового сервера',content:'Тестовая фикстура, не результат реального AI.\nДальше: проверить источники.'}});}
      if(suffix==='/projects/server-project/artifacts') {saveIds.push(req.headers()['idempotency-key']); return json(++saveAttempts===1 ? {saved:false} : {saved:true,artifactId:'artifact-1',version:'1',savedAt:'2026-10-15T00:00:00Z'});}
      if(suffix==='/projects/server-project/artifacts/artifact-1/zip') return route.fulfill({status:200,contentType:'application/zip',body:Buffer.from([80,75,5,6,...Array(18).fill(0)])});
      throw new Error('Unexpected fixture route '+suffix);
    });
    await page.goto(FIXTURE_URL); await page.getByLabel('Новый проект').fill('Fixture business decision'); await page.getByRole('button',{name:/Создать проект/}).click();
    await page.getByLabel('Задача, факты и ссылки').fill('Fixture context');
    await page.getByRole('button',{name:'Подключить проект к сервису'}).click();
    await page.screenshot({path:path.join(ROOT,'test-results','fixture-connect-diagnostic.png'),fullPage:true}); console.log('Fixture after connect:', await page.locator('main').innerText());
    await page.getByRole('button',{name:'Запустить задачу'}).click();
    await page.getByText('В работе',{exact:true}).waitFor(); await page.getByRole('button',{name:'Проверить статус'}).click();
    await page.getByText('Какие ограничения важны?').waitFor(); await page.getByLabel('Ваш ответ').fill('Fixture limits'); await page.getByRole('button',{name:'Ответить и продолжить'}).click();
    await page.getByRole('heading',{name:'Результат тестового сервера'}).waitFor();
    await page.getByRole('button',{name:'Сохранить результат на сервере'}).click(); await page.getByRole('alert').filter({hasText:'Сохранение не подтверждено'}).waitFor();
    assert.equal(metrics.filter(e=>e.name==='artifact_saved').length,0); assert.equal(await page.getByRole('button',{name:'Скачать ZIP ↓'}).count(),0);
    await page.reload(); await page.getByRole('button',{name:/Fixture business decision/}).click(); await page.getByRole('button',{name:'Повторить запрос'}).click();
    await page.getByText(/Сервер подтвердил сохранение · версия 1/).waitFor(); assert.equal(saveIds.length,2); assert.equal(saveIds[0],saveIds[1]);
    const pending=page.waitForEvent('download'); await page.getByRole('button',{name:'Скачать ZIP ↓'}).click(); const dl=await pending; assert.equal(dl.suggestedFilename(),'coreteams-result.zip');
    await page.screenshot({path:path.join(ROOT,'test-results','desktop-fixture-result.png'),fullPage:true});
    assert.equal(metrics.filter(e=>e.name==='artifact_saved').length,1); assert.equal(metrics.filter(e=>e.name==='artifact_downloaded').length,1);
    assert.equal(metrics.find(e=>e.name==='artifact_saved').runId,'run-1'); assert.equal(metrics.find(e=>e.name==='artifact_saved').artifactId,'artifact-1'); assert.ok(metrics.every(e=>e.buildVersion));
    await page.getByRole('button',{name:'← Все проекты'}).click(); await page.getByRole('button',{name:/Fixture business decision/}).click(); await page.getByRole('button',{name:'Скачать ZIP ↓'}).waitFor();
    console.log('PASS fixture: server running/question/answer/result, invalid save ack blocked, reload retry stable ID, acknowledged save, ZIP and reopen');
    await ctx.close();
    const corruptCtx=await browser.newContext(); const corrupt=await corruptCtx.newPage(); await corrupt.goto(DEFAULT_URL); await corrupt.evaluate(()=>localStorage.setItem('coreteams-workspace-v1','{invalid')); await corrupt.reload(); await corrupt.getByRole('alert').filter({hasText:'не перезаписаны'}).waitFor(); assert.equal(await corrupt.evaluate(()=>localStorage.getItem('coreteams-workspace-v1')),'{invalid'); await corruptCtx.close(); console.log('PASS corrupt storage preserved and edits blocked');
    const cross=await browser.newContext(); const first=await cross.newPage(),second=await cross.newPage(); await first.goto(DEFAULT_URL); await second.goto(DEFAULT_URL); await first.getByLabel('Новый проект').fill('Cross tab'); await first.getByRole('button',{name:/Создать проект/}).click(); await second.getByRole('alert').filter({hasText:'другой вкладке'}).waitFor(); assert.equal(await second.getByRole('button',{name:/Создать проект/}).isDisabled(),true); await cross.close(); console.log('PASS stale tab mutation blocked');
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
