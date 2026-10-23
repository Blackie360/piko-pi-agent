// All requests and media are fixtures. Never connects to the Pi or OpenAI.
const { chromium } = require(process.env.PI_AGENT_PLAYWRIGHT_MODULE || 'playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = process.env.PI_AGENT_SOURCE || __dirname;
const output = process.env.PI_AGENT_QA_DIR || path.join(__dirname, 'qa-artifacts');
fs.mkdirSync(output, {recursive:true});
const csp = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'";
const errors=[], unexpected=[], posts=[], checks=[];
let signedIn=false, phase='running';
const isActive=()=>['running','waiting','cancelling'].includes(phase);
const state=()=>({configured:true,model:'fixture-model',auto_commands:false,auto_sudo:false,models:[{id:'fixture-model',label:'Fixture model',group:'Test'}],chats:[{id:'fixture-chat',title:'Investigate Pi health'}],active:isActive()?['fixture-run']:[],active_sessions:isActive()?[{chat:'fixture-chat',channel:'dashboard'}]:[]});
const metrics={totals:{input:100,output:50,total:150,requests:3},last24h:{tokens:150,requests:3},models:[{model:'fixture',input:100,output:50,total:150,requests:3}],tasks:[{status:'complete',count:1}],average_seconds:2.5,recent:[],pi:{load:[0.4,0.3,0.2],cpus:4,memory_total:4*1073741824,memory_used:1073741824,disk_total:64*1073741824,disk_used:16*1073741824,uptime_seconds:90061},active:0,tracking_since:1791576000};
const devices={devices:[{name:'Pi host',hostname:'pi.fixture.test',os:'linux',ip:'100.64.0.1',online:true,is_pi:true},{name:'Fixture laptop',hostname:'laptop.fixture.test',os:'macOS',ip:'100.64.0.2',online:true,is_pi:false}],connected:true,checked_at:1791576000};
const chat=()=>({messages:[{id:1,role:'user',text:'Inspect the Pi and research the recommended next step.'},...(!isActive()?[{id:2,role:'assistant',text:'**Combined findings** are ready. No changes were made.'}]:[])],runs:[{
 id:'fixture-run',status:phase,
 workers:[{id:'worker-one',slot:1,task:'Inspect system health and identify resource constraints.',status:phase==='running'?'running':phase==='cancelled'?'cancelled':'complete',result:phase==='running'?null:'**Verified health** with [documentation](https://example.com).\n\n<img src=x onerror=alert(1)>',error:null},
 {id:'worker-two',slot:2,task:'Research official guidance and compare possible next steps.',status:phase==='running'?'running':phase==='partial'?'failed':phase==='cancelled'?'cancelled':'complete',result:['complete','waiting','cancelling'].includes(phase)?'Research is complete. These are proposals, not executed changes.':null,error:phase==='partial'?'Fixture provider error. No result was returned.':null}],
 events:[{kind:'info',text:'Coordinator delegated two parallel investigations.'},...(phase==='waiting'?[{kind:'approval',approval_id:'fixture-approval',command:'printf fixture',reason:'Synthetic approval only; no command is executed.',cwd:'/fixture'}]:[]),...(phase==='complete'?[{kind:'decision',approval_id:'fixture-approval',approved:true}]:[])]
}]});

(async()=>{
 const browser=await chromium.launch({headless:true,...(process.env.PI_AGENT_CHROMIUM?{executablePath:process.env.PI_AGENT_CHROMIUM}:{})});
 try{
  const context=await browser.newContext({viewport:{width:1440,height:1000},reducedMotion:'reduce'});
  await context.addInitScript(()=>{
   window.fixtureMedia={starts:0,tracks:[],peers:[],audioRequests:0};
   Object.defineProperty(navigator,'mediaDevices',{value:{getUserMedia:async()=>{
    fixtureMedia.starts++;
    const track={enabled:true,stopped:false,stop(){this.stopped=true;}};
    fixtureMedia.tracks.push(track);
    return {getTracks:()=>[track],getAudioTracks:()=>[track]};
   }}});
   window.RTCPeerConnection=class{
    constructor(){fixtureMedia.peers.push(this);this.connectionState='new';}
    addTrack(){}
    createDataChannel(){this.channel={readyState:'open',send(){},close(){this.readyState='closed';}};return this.channel;}
    async createOffer(){return {type:'offer',sdp:'fixture-sdp'};}
    async setLocalDescription(d){this.localDescription=d;}
    async setRemoteDescription(){this.connectionState='connected';this.channel.onopen?.();this.channel.onmessage?.({data:JSON.stringify({type:'session.created'})});}
    close(){this.connectionState='closed';this.onconnectionstatechange?.();}
   };
   window.AudioContext=class{constructor(){throw Error('Audio analysis disabled in fixture');}};
   window.webkitAudioContext=window.AudioContext;
   window.Audio=class{async play(){fixtureMedia.audioRequests++;}pause(){}};
   HTMLMediaElement.prototype.play=async function(){};
   HTMLMediaElement.prototype.pause=function(){};
  });
  await context.route('**/*',async route=>{
   const request=route.request(),u=new URL(request.url());
   if(u.origin!=='https://pi-agent.test'){unexpected.push(request.url());return route.abort();}
   const headers={'Content-Security-Policy':csp,'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'};
   const json=(data,status=200)=>route.fulfill({status,contentType:'application/json',headers,body:JSON.stringify(data)});
   if(u.pathname.startsWith('/api/')){
    if(request.method()!=='GET'){
     posts.push({path:u.pathname,body:request.postDataJSON()});
     if(u.pathname==='/api/login'){signedIn=true;return json({ok:true});}
     if(u.pathname==='/api/approvals/fixture-approval'){phase=request.postDataJSON().approved?'complete':'cancelled';return json({ok:true});}
     if(u.pathname==='/api/runs/fixture-run/stop'){phase='cancelled';return json({ok:true});}
     if(u.pathname==='/api/voice/live')return route.fulfill({status:200,contentType:'application/sdp',headers,body:'fixture-sdp'});
     if(u.pathname.startsWith('/api/voice/speak/'))return route.fulfill({status:200,contentType:'audio/mpeg',headers,body:'fixture-audio'});
     unexpected.push(request.method()+' '+u.pathname);return route.abort();
    }
    if(!signedIn)return json({detail:'Please sign in'},401);
    const data=u.pathname==='/api/state'?state():u.pathname==='/api/metrics'?{...metrics,active:isActive()?1:0}:u.pathname==='/api/devices'?devices:u.pathname==='/api/chats/fixture-chat'?chat():null;
    if(data===null){unexpected.push(u.pathname);return route.abort();}
    return json(data);
   }
   const file=path.resolve(root,u.pathname==='/'?'static/index.html':u.pathname.slice(1));
   if(!file.startsWith(root+path.sep)||!fs.existsSync(file)){unexpected.push(u.pathname);return route.abort();}
   return route.fulfill({status:200,headers,contentType:{'.html':'text/html','.css':'text/css','.js':'text/javascript','.svg':'image/svg+xml','.png':'image/png','.ico':'image/x-icon','.ttf':'font/ttf'}[path.extname(file)]||'application/octet-stream',body:fs.readFileSync(file)});
  });
  const page=await context.newPage();
  page.on('pageerror',e=>errors.push(e.message));
  page.on('console',m=>{if(/Content Security Policy|Refused to|Uncaught/.test(m.text()))errors.push(m.text());});
  await page.goto('https://pi-agent.test/');
  assert(await page.locator('#login').isVisible());
  assert.equal(await page.evaluate(()=>fixtureMedia.starts),0);
  await page.locator('#password').fill('synthetic-access-code');await page.locator('#loginForm button').click();
  await page.locator('#shell').waitFor({state:'visible'});
  await page.waitForFunction(()=>document.querySelectorAll('.worker').length===2);
  await page.evaluate(()=>document.fonts.ready);
  assert(await page.evaluate(()=>document.fonts.check('16px Geist')&&document.fonts.check('16px "Instrument Serif"')));
  assert.equal(await page.locator('link[rel="icon"][type="image/svg+xml"]').getAttribute('href'),'/static/favicon.svg');
  assert.equal(await page.locator('.worker[data-state="running"]').count(),2);
  assert(await page.locator('#send').isDisabled());assert(await page.locator('#modelPicker').isDisabled());
  assert.equal(await page.evaluate(()=>fixtureMedia.starts),0);
  await page.screenshot({path:path.join(output,'integrated-desktop.png'),fullPage:true});
  checks.push('Local fonts, preserved SVG favicon, two worker cards, active-task controls, idle microphone');
  phase='waiting';await page.evaluate(()=>refresh());
  await page.locator('.hold').waitFor();
  const approvalCount=()=>posts.filter(p=>p.path.includes('/approvals/')).length;
  const hold=page.locator('.hold');
  await hold.hover();await page.mouse.down();await page.waitForTimeout(100);await page.mouse.up();
  assert.equal(approvalCount(),0);
  await hold.hover();await page.mouse.down();await page.waitForTimeout(950);await page.mouse.up();
  await page.waitForFunction(()=>!document.querySelector('.hold'));
  assert.equal(approvalCount(),1);
  phase='waiting';await page.evaluate(()=>refresh());await page.getByRole('button',{name:'Decline',exact:true}).click();
  assert.equal(approvalCount(),2);assert.equal(posts.filter(p=>p.path.includes('/approvals/')).at(-1).body.approved,false);
  checks.push('Short click does not approve; hold approves once; decline stays independent');
  phase='partial';await page.evaluate(()=>refresh());
  assert.equal(await page.locator('.worker[data-state="failed"]').count(),1);
  await page.locator('.worker details summary').click();
  assert.equal(await page.locator('.worker .body img').count(),0);
  assert.equal(await page.locator('.worker .body strong').textContent(),'Verified health');
  assert((await page.locator('.worker .body a').getAttribute('rel')).includes('noopener'));
  checks.push('Partial failure, retained findings, safe Markdown rendering');
  phase='cancelling';await page.evaluate(()=>refresh());
  assert.equal(await page.locator('#status').textContent(),'Stopping…');assert(await page.locator('#stop').isDisabled());
  phase='running';await page.evaluate(()=>refresh());await page.locator('#stop').click();
  await page.waitForFunction(()=>document.querySelectorAll('.worker[data-state="cancelled"]').length===2);
  checks.push('Cancelling state, stop control, both workers visibly stopped');
  await page.locator('#metricsOpen').click();await page.waitForFunction(()=>document.querySelectorAll('.metricCard').length===4);await page.locator('#metricsClose').click();
  await page.locator('#devicesOpen').click();await page.waitForFunction(()=>document.querySelectorAll('.deviceCard').length===2);await page.locator('#devicesClose').click();
  checks.push('Dashboard and Tailscale device fixtures');
  await page.locator('#liveVoice').click();await page.locator('#liveMute').waitFor({state:'visible'});
  assert.equal(await page.evaluate(()=>fixtureMedia.starts),1);
  await page.locator('#liveMute').click();assert.equal(await page.evaluate(()=>fixtureMedia.tracks[0].enabled),false);
  await page.locator('#liveMute').click();assert.equal(await page.evaluate(()=>fixtureMedia.tracks[0].enabled),true);
  await page.locator('#liveVoice').click();assert(await page.evaluate(()=>fixtureMedia.tracks.every(t=>t.stopped)));
  await page.locator('#liveVoice').click();await page.locator('#liveMute').waitFor({state:'visible'});
  await page.evaluate(()=>{const p=fixtureMedia.peers.at(-1);p.connectionState='disconnected';p.onconnectionstatechange();});
  assert(await page.evaluate(()=>fixtureMedia.tracks.every(t=>t.stopped)));
  await page.locator('#liveVoice').click();await page.locator('#liveMute').waitFor({state:'visible'});
  await page.evaluate(()=>window.dispatchEvent(new Event('pagehide')));
  assert(await page.evaluate(()=>fixtureMedia.tracks.every(t=>t.stopped)));
  checks.push('Synthetic live voice: explicit start, mute/unmute, end/disconnect/page exit stop all tracks');
  phase='partial';await page.evaluate(()=>refresh());
  for(const width of [768,390,320]){
   await page.setViewportSize({width,height:width===320?568:844});
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Page overflow at '+width);
   await page.locator('#railToggle').click();await page.locator('#settingsOpen').click();
   assert(await page.locator('#settings').isVisible());
   const bounds=await page.locator('#settings').boundingBox();assert(bounds.x>=0&&bounds.x+bounds.width<=width+1);
   await page.locator('#settingsClose').click();
  }
  await page.locator('.worker[data-state="failed"]').scrollIntoViewIfNeeded();
  for(const card of await page.locator('.worker').all()){
   const bounds=await card.boundingBox();assert(bounds.x>=0&&bounds.x+bounds.width<=321,'Worker card overflow');
  }
  await page.screenshot({path:path.join(output,'integrated-mobile.png'),fullPage:true});
  checks.push('768/390/320 px responsive layout and settings navigation');
  assert.deepEqual(errors,[]);assert.deepEqual(unexpected,[]);
  const evidence={passed:true,checks,realMicrophoneCaptures:0,productionRequests:0,errors,unexpected};
  fs.writeFileSync(path.join(output,'browser-results.json'),JSON.stringify(evidence,null,2)+'\n');
  console.log(JSON.stringify(evidence,null,2));
 }finally{await browser.close();}
})().catch(e=>{console.error(e.stack);process.exitCode=1;});

