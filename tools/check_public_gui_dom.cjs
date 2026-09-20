/* Inspect only the freshly-created read-only diagnostic host, never a user tab. */
'use strict';
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require('playwright');
const session=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const output=path.resolve(process.argv[3]),expected=process.argv[4];
fs.mkdirSync(output,{recursive:false});
if(session.read_only!==true||session.control_enabled!==false||!/^http:\/\/127\.0\.0\.1:\d+$/.test(session.origin))throw Error('Read-only diagnostic session required');
(async()=>{
 let browser,errors=[],blocked=[];
 try{
  browser=await chromium.launch({headless:true,channel:'msedge'});const page=await browser.newPage({viewport:{width:1280,height:960}});
  await page.route('**/*',route=>{
   const req=route.request(),url=new URL(req.url());
   const reads=['/setup-api/status','/setup-api/setup-preferences','/setup-api/launcher-status'];
   if(url.origin!==session.origin||(req.method()==='POST'&&!reads.includes(url.pathname))){blocked.push({method:req.method(),path:url.pathname});return route.abort();}
   return route.continue();
  });
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto(session.url);
  await page.waitForFunction(()=>window.GKMS_SETUP_PREVIEW?.modulesReady()===true);
  assert.equal(await page.locator('#gameDir').inputValue(),expected);
  assert.equal(await page.evaluate(()=>window.GKMS_SETUP_PREVIEW.getPreferences().game_directory),expected);
  await page.screenshot({path:path.join(output,'stored-path-startup.png'),fullPage:true});
  assert.equal(await page.locator('#developerPanel').count(),0);
  assert.deepEqual(blocked,[]);assert.deepEqual(errors,[]);
 }catch(error){errors.push(error.message);process.exitCode=1;}
 finally{
  if(browser)await browser.close();
  fs.writeFileSync(path.join(output,'report.json'),JSON.stringify({schema:'gkms.isolated-public-stored-path-dom.v1',passed:errors.length===0,
    actual_readonly_host:true,configured_path_displayed:errors.length===0,private_token_printed:false,game_io:false,blocked_requests:blocked,errors},null,2));
 }
})();
