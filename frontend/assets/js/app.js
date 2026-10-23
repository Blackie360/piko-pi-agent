'use strict';
/* Piko · Mission Control front end. No framework, no build step, strict CSP (no inline styles/scripts). */
const $=id=>document.getElementById(id);
const SVG_NS='http://www.w3.org/2000/svg';
let current=null,state=null,run=null,last='',polling=false,primed=false;

/* ---------- plumbing ---------- */
async function api(path,body,method){
 let r;
 try{r=await fetch('/api/'+path,{method:method||(body===undefined?'GET':'POST'),headers:{'Content-Type':'application/json','X-Pi-Agent':'1'},body:body===undefined?undefined:JSON.stringify(body)});}
 catch{throw Error('Connection to the Pi was interrupted. Check Tailscale and try again.');}
 const d=await r.json();
 if(!r.ok){if(r.status===401){$('login').hidden=false;$('shell').hidden=true;primed=false;}throw Error(d.detail||'Request failed');}
 return d;
}
function node(tag,text,cls){const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(cls)el.className=cls;return el;}
function svg(tag,attrs){const el=document.createElementNS(SVG_NS,tag);for(const k in attrs)el.setAttribute(k,attrs[k]);return el;}
function error(e){$('error').textContent=e.message;}
const number=v=>Number(v||0).toLocaleString();
const gib=v=>(v/1073741824).toFixed(1)+' GB';
const reducedMotion=()=>matchMedia('(prefers-reduced-motion: reduce)').matches;
const railVisible=()=>matchMedia('(min-width: 1181px)').matches;

/* ---------- rich text: a tiny, DOM-only Markdown subset (code fences, inline code, bold, links) ---------- */
function safeLink(text,href){
 try{const u=new URL(href);if(!['https:','http:'].includes(u.protocol))return document.createTextNode(text);const a=node('a',text);a.href=u.href;a.target='_blank';a.rel='noopener noreferrer';return a;}
 catch{return document.createTextNode(text);}
}
function inline(el,text){
 const re=/\*\*([^*\n]+)\*\*|`([^`\n]+)`|\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s<>"')]+)/g;
 let i=0,m;
 while((m=re.exec(text))){
  if(m.index>i)el.append(text.slice(i,m.index));
  if(m[1]!==undefined)el.append(node('strong',m[1]));
  else if(m[2]!==undefined)el.append(node('code',m[2]));
  else if(m[3]!==undefined)el.append(safeLink(m[3],m[4]));
  else el.append(safeLink(m[5],m[5]));
  i=m.index+m[0].length;
 }
 if(i<text.length)el.append(text.slice(i));
}
function rich(text){return PikoMarkdown.render(text);}

/* ---------- hold-to-approve launch control ---------- */
function holdButton(label,onConfirm){
 const b=node('button',undefined,'hold');b.type='button';b.append(node('span',label),node('i'));
 let timer=null;
 const cancel=()=>{if(timer){clearTimeout(timer);timer=null;}b.classList.remove('holding');};
 const fire=async()=>{cancel();b.disabled=true;try{await onConfirm();}catch(err){error(err);b.disabled=false;}};
 b.onpointerdown=e=>{if(b.disabled||e.button)return;e.preventDefault();try{b.setPointerCapture(e.pointerId);}catch{}b.classList.add('holding');timer=setTimeout(fire,800);};
 b.onpointerup=b.onpointercancel=cancel;
 b.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();fire();}};
 b.title='Hold to approve';
 return b;
}

/* ---------- state + feed ---------- */
async function refresh(){
 state=await api('state');updateModels();
 $('welcomePolicy').textContent=state.auto_commands?'Give me a task. I’ll carry it out and show what I did.':'Ask a question or give me a task. You review commands before they run.';
 $('hint').textContent=state.auto_commands?'Ordinary commands run automatically · Sudo needs approval.':state.auto_sudo?'Commands need approval, including sudo.':'Commands run only after your approval.';
 $('login').hidden=true;$('shell').hidden=false;$('setupBanner').hidden=state.configured;
 if(!$('settings').open)$('model').value=state.model;
 $('chats').replaceChildren();
 for(const c of state.chats){
  const label=c.display_title||c.title;
  const row=node('div',undefined,'chatRow'),b=node('button',label,c.id===current?'selected':'');b.title=label;
  b.onclick=()=>switchChat(c.id);
  const del=node('button','×','deleteChat');del.title='Delete conversation';del.setAttribute('aria-label','Delete '+label);
  del.disabled=state.active_sessions.some(s=>s.chat===c.id);
  del.onclick=async()=>{if(!confirm('Delete this conversation and its history? Completed changes will remain.'))return;try{await api('chats/'+c.id,undefined,'DELETE');if(current===c.id){current=null;last='';}await refresh();}catch(e){error(e);}};
  row.append(b,del);$('chats').append(row);
 }
 if(!current&&state.chats.length)current=state.chats[0].id;
 if(!primed){primed=true;pollMetrics();refreshDevices();}
 $('send').disabled=state.active_sessions.some(s=>s.channel==='dashboard'||s.chat===current)||!state.configured;
 await render();
}
function switchChat(id){
 if(id===current){closeRail();return;}
 current=id;last='';closeRail();
 const go=()=>refresh().catch(error);
 if(document.startViewTransition&&!reducedMotion())document.startViewTransition(go);else go();
}
async function render(){
 if(!current){$('messages').replaceChildren();$('activity').replaceChildren();$('welcome').hidden=false;setStatus(null);return;}
 const d=await api('chats/'+current);
 const signature=JSON.stringify(d);if(signature===last)return;last=signature;
 const feed=$('feed');const atBottom=feed.scrollHeight-feed.scrollTop-feed.clientHeight<100;
 $('messages').replaceChildren();
 for(const m of d.messages){
  const el=node('div',undefined,'message '+m.role);
  el.append(node('span',m.role==='user'?'You':'Piko','role'));
  if(m.role==='assistant'){el.append(rich(m.text));const b=node('button','▶ Listen','listen');b.type='button';b.onclick=()=>playReply(m.id).catch(error);el.append(b);}
  else el.append(node('div',m.text,'text'));
  $('messages').append(el);
 }
 $('welcome').hidden=!!d.messages.length;
 $('activity').replaceChildren();
 run=d.runs.find(r=>['running','waiting','cancelling'].includes(r.status));
 setStatus(run);
 $('stop').hidden=!run;$('stop').disabled=run?.status==='cancelling';$('stop').textContent=run?.status==='cancelling'?'Stopping…':'Stop task';
 for(const r of d.runs){
  const decisions=new Map(r.events.filter(e=>e.kind==='decision').map(e=>[e.approval_id,e.approved]));
  const box=node('section');
  const workers=r.workers||[];
  if(workers.length){
   const group=node('div',undefined,'workers');group.setAttribute('aria-label','Parallel workers');
   for(const worker of workers){
    const card=node('div',undefined,'worker');card.dataset.state=worker.status;
    const heading=node('div',undefined,'workerHead');
    const labels={queued:'Queued',running:'Working',complete:'Complete',failed:'Failed',cancelled:'Stopped',interrupted:'Interrupted'};
    heading.append(node('strong','Worker '+worker.slot),node('span',labels[worker.status]||worker.status,'badge'));
    card.append(heading,node('p',worker.task));
    if(worker.error)card.append(node('p',worker.error,'error'));
    if(worker.result){const detail=node('details');detail.append(node('summary','View findings'),rich(worker.result));card.append(detail);}
    group.append(card);
   }
   box.append(group);
  }
  for(const command of r.commands||[]){
   const detail=node('details');detail.append(node('summary','Command '+command.id.slice(0,8)+' · '+command.status),node('pre',command.command),node('p','Approval: '+command.approval_source+' · Exit: '+(command.exit_code??'unknown')+' · Signal: '+(command.signal??'none')+' · Observed output bytes: '+command.output_bytes+(command.truncated?' · output truncated':'')),node('p',command.diff_coverage||'No diff coverage'),node('pre',command.diff||''));box.append(detail);
  }
  for(const e of r.events){
   if(e.kind==='decision')continue;
   const el=node('div',undefined,'event '+e.kind);
   if(e.kind==='approval'){
    el.append(node('strong','Command approval'),node('p',e.reason),node('pre',e.command),node('small','Working directory: '+e.cwd));
    if(decisions.has(e.approval_id)){const ok=decisions.get(e.approval_id);el.append(node('p',ok?'Approved · launched':'Declined','verdict '+(ok?'yes':'no')));}
    else if(['running','waiting'].includes(r.status)){
     const actions=node('div',undefined,'actions');
     const decide=async approved=>{await api('approvals/'+e.approval_id,{approved});last='';await render();};
     const no=node('button','Decline','quiet');no.type='button';no.onclick=async()=>{no.disabled=true;try{await decide(false);}catch(err){error(err);no.disabled=false;}};
     const listen=node('button','Listen to exact command','quiet');listen.type='button';listen.onclick=()=>playApproval(e.approval_id).catch(error);actions.append(listen);
     actions.append(no,holdButton('Hold to approve & run',()=>decide(true)));
     el.append(actions);
    }else el.append(node('p','No longer pending','verdict'));
   }else if(e.kind==='sources'){
    el.append(node('strong','Web sources'));
    for(const source of e.sources||[]){try{const u=new URL(source.url);if(!['https:','http:'].includes(u.protocol))continue;el.append(safeLink(source.title||u.hostname,u.href));}catch{}}
   }else if(e.kind==='info'){el.append(node('span',e.text||'','eventText'));}
   else el.textContent=e.text||'';
   if(e.worker_id){const worker=workers.find(w=>w.id===e.worker_id);el.prepend(node('small','Worker '+(worker?.slot||'?')+' · ','workerSource'));}
   box.append(el);
  }
  if(r.events.length||workers.length){box.append(node('small','Task '+r.status));$('activity').append(box);}
 }
 if(atBottom)feed.scrollTop=feed.scrollHeight;
 maybeReadReply(d);
}
function setStatus(r){
 const s=$('status');
 s.textContent=r?(r.status==='waiting'?'Needs approval':r.status==='cancelling'?'Stopping…':'Working'):'Ready';
 s.dataset.state=r?r.status:'ready';
 if(!livePeer)setOrb(orbVoice||(r?'working':'idle'));
}

/* ---------- controls ---------- */
$('loginForm').onsubmit=async e=>{e.preventDefault();try{await api('login',{password:$('password').value});$('password').value='';await refresh();}catch(err){$('loginError').textContent=err.message;}};
async function newChat(){try{current=(await api('chats',{})).id;last='';await refresh();closeRail();$('prompt').focus();}catch(e){error(e);}}
$('newChat').onclick=newChat;
document.addEventListener('keydown',e=>{if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='k'&&!$('shell').hidden){e.preventDefault();newChat();}});
function openSettings(){closeRail();$('settingsError').textContent='';$('settings').showModal();}
$('settingsOpen').onclick=openSettings;$('connect').onclick=openSettings;$('settingsClose').onclick=()=>$('settings').close();
$('settingsForm').onsubmit=async e=>{
 e.preventDefault();$('saveSettings').disabled=true;$('settingsError').textContent='Verifying connection…';
 try{await api('settings',{key:$('apiKey').value,model:$('model').value});$('apiKey').value='';$('settings').close();await refresh();}
 catch(err){$('settingsError').textContent=err.message;}
 finally{$('saveSettings').disabled=false;}
};
$('composer').onsubmit=async e=>{
 e.preventDefault();$('error').textContent='';
 try{if(!current)current=(await api('chats',{})).id;await api('chats/'+current+'/send',{text:$('prompt').value,audit_workspace:$('auditWorkspace').value.trim()||null});$('prompt').value='';autosize();last='';await refresh();}
 catch(err){error(err);}
};
$('prompt').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();if(!$('send').disabled)$('composer').requestSubmit();}};
function autosize(){const t=$('prompt');t.style.height='auto';t.style.height=Math.min(220,t.scrollHeight)+'px';}
$('prompt').oninput=autosize;
$('stop').onclick=async()=>{if(run){try{await api('runs/'+run.id+'/stop',{});last='';await refresh();}catch(e){error(e);}}};
$('logout').onclick=async()=>{endLive();stopVoice();await api('logout',{});$('login').hidden=false;$('shell').hidden=true;current=null;primed=false;closeRail();};
document.querySelectorAll('[data-prompt]').forEach(b=>b.onclick=()=>{$('prompt').value=b.dataset.prompt;autosize();$('prompt').focus();});
refresh().catch(()=>{});
setInterval(async()=>{if(polling||$('shell').hidden)return;polling=true;try{await refresh();}catch(e){error(e);}finally{polling=false;}},2000);

/* mobile rail drawer */
function closeRail(){$('shell').classList.remove('railOpen');$('railToggle').setAttribute('aria-expanded','false');}
$('railToggle').onclick=()=>{const open=!$('shell').classList.contains('railOpen');$('shell').classList.toggle('railOpen',open);$('railToggle').setAttribute('aria-expanded',String(open));};
$('railScrim').onclick=closeRail;

/* ---------- voice orb ---------- */
let orbVoice=null,meterCtx=null,meterRaf=0;
function setOrb(s){$('orb').dataset.state=s;}
function startMeter(stream){
 try{
  meterCtx=new (window.AudioContext||window.webkitAudioContext)();
  const analyser=meterCtx.createAnalyser();analyser.fftSize=256;meterCtx.createMediaStreamSource(stream).connect(analyser);
  const data=new Uint8Array(analyser.frequencyBinCount);
  const tick=()=>{analyser.getByteTimeDomainData(data);let sum=0;for(const v of data){const d=(v-128)/128;sum+=d*d;}const rms=Math.sqrt(sum/data.length);$('orb').style.setProperty('--level',Math.min(1,rms*4).toFixed(3));meterRaf=requestAnimationFrame(tick);};
  tick();
 }catch{}
}
function stopMeter(){cancelAnimationFrame(meterRaf);meterRaf=0;meterCtx?.close().catch(()=>{});meterCtx=null;$('orb').style.removeProperty('--level');}

/* ---------- spoken replies ---------- */
let voiceBusy=false,player=null,audioURL=null,voiceEpoch=0;
const observedReplies=new Map();
function voiceStatus(text){$('voiceStatus').textContent=text;}
function stopVoice(){voiceEpoch++;if(player){player.pause();player=null;}if(audioURL){URL.revokeObjectURL(audioURL);audioURL=null;}$('voiceStop').hidden=true;orbVoice=null;if(!livePeer)setOrb(run?'working':'idle');}
async function voiceResponse(path,body,mime){
 let r;
 try{r=await fetch('/api/voice/'+path,{method:'POST',headers:{'X-Pi-Agent':'1','Content-Type':mime||'application/json'},body:body||'{}'});}
 catch{throw Error('Connection to the Pi was interrupted. Try again.');}
 if(!r.ok){const d=await r.json();throw Error(d.detail||'Voice request failed');}
 return r;
}
async function playReply(mid){
 if(voiceBusy)throw Error('Please wait for the current voice request.');
 stopVoice();const epoch=voiceEpoch;voiceBusy=true;$('voiceStop').hidden=false;voiceStatus('Preparing AI voice…');orbVoice='connecting';setOrb('connecting');
 try{
  const r=await voiceResponse('speak/'+mid);const blob=await r.blob();if(epoch!==voiceEpoch)return;
  audioURL=URL.createObjectURL(blob);player=new Audio(audioURL);
  player.onended=()=>{stopVoice();voiceStatus('Ready to speak.');};
  await player.play();orbVoice='speaking';setOrb('speaking');voiceStatus('Speaking with an AI-generated voice…');
 }catch(e){stopVoice();voiceStatus('Tap Listen to play the reply.');throw Error(e.name==='NotAllowedError'?'Your browser needs a tap on Listen to enable audio playback.':e.message);}
 finally{voiceBusy=false;}
}
async function playApproval(aid){
 if(voiceBusy)throw Error('Please wait for the current voice request.');
 stopVoice();const epoch=voiceEpoch;voiceBusy=true;$('voiceStop').hidden=false;
 try{
  const response=await voiceResponse('approval/'+aid);const blob=await response.blob();
  if(epoch!==voiceEpoch)return;
  audioURL=URL.createObjectURL(blob);player=new Audio(audioURL);
  player.onended=()=>{stopVoice();voiceStatus('Readback finished. Use the approval card if you want to run this command.');};
  await player.play();voiceStatus('Reading the pending command. Listening does not approve it.');
 }catch(e){stopVoice();throw e;}finally{voiceBusy=false;}
}
function maybeReadReply(chat){
 const replies=chat.messages.filter(m=>m.role==='assistant');const newest=replies.at(-1);
 const previous=observedReplies.get(current);observedReplies.set(current,newest?.id||0);
 if(!livePeer&&previous!==undefined&&newest&&newest.id>previous&&$('autoVoice').checked)playReply(newest.id).catch(error);
}
$('voiceStop').onclick=()=>{stopVoice();const task=voiceTaskIdentity();if(task){voiceControl.interrupt(task,Date.now(),false);showVoiceQuestion(false);}else voiceStatus('Voice stopped.');};
$('autoVoice').onchange=()=>{if(!$('autoVoice').checked)stopVoice();};
window.addEventListener('pagehide',stopVoice);

/* ---------- model picker ---------- */
let switchingModel=false,modelCatalog='';
function updateModels(){
 const tone=$('personaPicker');tone.replaceChildren();
 for(const item of state.personas||[]){const option=node('option',item.label);option.value=item.id;tone.append(option);}
 tone.value=state.persona||'warm';tone.disabled=state.active.length>0;
 const picker=$('modelPicker');const signature=JSON.stringify(state.models);
 if(signature!==modelCatalog){
  picker.replaceChildren();const groups=new Map();
  for(const m of state.models||[]){
   if(!groups.has(m.group)){const group=document.createElement('optgroup');group.label=m.group;groups.set(m.group,group);picker.append(group);}
   const option=node('option',m.label);option.value=m.id;groups.get(m.group).append(option);
  }
  modelCatalog=signature;
 }
 if(!switchingModel)picker.value=state.model;
 picker.disabled=switchingModel||!state.configured||state.active.length>0;
}
$('personaPicker').onchange=async()=>{try{await api('persona',{persona:$('personaPicker').value});await refresh();}catch(e){error(e);}};
$('modelPicker').onchange=async()=>{
 const chosen=$('modelPicker').value;switchingModel=true;$('modelPicker').disabled=true;$('error').textContent='';
 try{await api('model',{model:chosen});voiceStatus('Model changed. Spoken replies remain enabled.');await refresh();}
 catch(e){error(e);}
 finally{switchingModel=false;await refresh().catch(error);}
};

/* ---------- telemetry: rail + dashboard share one poll ---------- */
const SPARK_MAX=48,loadSamples=[];let metricsBusy=false;
function fmtUptime(s){const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);return d?d+'d '+h+'h':h?h+'h '+m+'m':m+'m';}
function gauge(id,pct,label){
 const ring=$(id).querySelector('.ring');const v=Math.max(0,Math.min(100,pct||0));
 ring.style.setProperty('--v',v.toFixed(0));ring.classList.toggle('warn',v>=70&&v<90);ring.classList.toggle('hot',v>=90);
 ring.querySelector('strong').textContent=label;
}
function paintSpark(){
 const el=$('sparkline');el.replaceChildren();if(loadSamples.length<2)return;
 const w=240,h=56,max=Math.max(0.5,...loadSamples)*1.15,off=SPARK_MAX-loadSamples.length;
 const pts=loadSamples.map((v,i)=>[((i+off)/(SPARK_MAX-1))*w,h-3-(v/max)*(h-10)]);
 const d=pts.map((p,i)=>(i?'L':'M')+p[0].toFixed(1)+' '+p[1].toFixed(1)).join(' ');
 el.append(svg('path',{class:'area',d:d+' L'+w+' '+h+' L'+pts[0][0].toFixed(1)+' '+h+' Z'}),svg('path',{class:'line',d}));
 const tip=pts.at(-1);el.append(svg('circle',{class:'tip',cx:tip[0].toFixed(1),cy:tip[1].toFixed(1),r:2.5}));
}
function paintVitals(d){
 const p=d.pi,load=p.load[0];
 gauge('gCpu',load/p.cpus*100,load.toFixed(2));
 gauge('gMem',p.memory_used/p.memory_total*100,Math.round(p.memory_used/p.memory_total*100)+'%');
 gauge('gDisk',p.disk_used/p.disk_total*100,Math.round(p.disk_used/p.disk_total*100)+'%');
 loadSamples.push(load);if(loadSamples.length>SPARK_MAX)loadSamples.shift();paintSpark();
 $('sparkNow').textContent=load.toFixed(2);
 $('vUptime').textContent=fmtUptime(p.uptime_seconds);$('vTokens').textContent=number(d.last24h.tokens);$('vActive').textContent=String(d.active);
 $('vitalsNote').textContent='Updated '+new Date().toLocaleTimeString();
}
function metricsTable(target,head,rows){
 const t=node('table',undefined,'metricsTable');const hr=node('tr');head.forEach(h=>hr.append(node('th',h)));t.append(hr);
 for(const row of rows){const tr=node('tr');row.forEach(v=>tr.append(node('td',String(v))));t.append(tr);}
 $(target).replaceChildren(t);
}
function paintDashboard(d){
 $('metricCards').replaceChildren();
 const completed=d.tasks.find(t=>t.status==='complete')?.count||0;const failed=d.tasks.find(t=>t.status==='failed')?.count||0;
 for(const [label,value,detail] of [
  ['Total tokens',number(d.totals.total),'Input '+number(d.totals.input)+' · Output '+number(d.totals.output)],
  ['Last 24 hours',number(d.last24h.tokens),number(d.last24h.requests)+' model requests'],
  ['Completed tasks',number(completed),number(failed)+' failed · '+d.active+' active'],
  ['Average response',d.average_seconds===null?'—':d.average_seconds.toFixed(1)+'s','Completed tasks since tracking began']]){
  const c=node('div',undefined,'metricCard');c.append(node('small',label),node('strong',value),node('small',detail));$('metricCards').append(c);
 }
 $('piMetrics').replaceChildren();const p=d.pi;
 for(const [label,used,total,detail] of [
  ['Memory',p.memory_used,p.memory_total,gib(p.memory_used)+' / '+gib(p.memory_total)],
  ['Disk',p.disk_used,p.disk_total,gib(p.disk_used)+' / '+gib(p.disk_total)],
  ['CPU load (1 minute)',p.load[0],p.cpus,p.load[0].toFixed(2)+' across '+p.cpus+' cores · up '+fmtUptime(p.uptime_seconds)]]){
  const c=node('div',undefined,'piMetric');const bar=node('progress');bar.max=total;bar.value=used;c.append(node('strong',label),bar,node('small',detail));$('piMetrics').append(c);
 }
 metricsTable('modelMetrics',['Model','Input','Output','Total tokens','Requests'],d.models.map(m=>[m.model,number(m.input),number(m.output),number(m.total),number(m.requests)]));
 metricsTable('taskMetrics',['Time','Model','Duration','Result'],d.recent.map(t=>[new Date(t.created*1000).toLocaleTimeString(),t.model,t.duration.toFixed(1)+'s',t.status]));
 $('metricsNote').textContent=d.models.length?'Local token tracking since '+new Date(d.tracking_since*1000).toLocaleString()+'. Voice billing and account-wide usage are separate.':'Token tracking starts with your next task. Previous usage was not recorded. Voice billing and account-wide usage are separate.';
}
async function pollMetrics(){
 if(metricsBusy||$('shell').hidden)return;
 if(!railVisible()&&!$('metrics').open)return;
 metricsBusy=true;
 try{const d=await api('metrics');paintVitals(d);if($('metrics').open){paintDashboard(d);$('metricsError').textContent='';}}
 catch(e){if($('metrics').open)$('metricsError').textContent=e.message;else $('vitalsNote').textContent='Telemetry unavailable · '+e.message;}
 finally{metricsBusy=false;}
}
$('metricsOpen').onclick=()=>{closeRail();$('metrics').showModal();pollMetrics();};
$('metricsClose').onclick=()=>$('metrics').close();
setInterval(pollMetrics,5000);

/* ---------- live conversation (WebRTC) ---------- */
let livePeer=null,liveStream=null,liveChannel=null,liveTimer=null,liveStarting=false;
let liveWork=null,liveSubmitting=false,liveResponding=false;
const voiceControl=new VoiceTaskControl();
const liveSeenInputs=new Set(),liveSeenCalls=new Set(),liveSpeechTurns=new Map();
function voiceTaskIdentity(){
 if(liveWork)return {...liveWork};
 return run&&['running','waiting','cancelling'].includes(run.status)?{rid:run.id,cid:current}:null;
}
function clearVoiceQuestion(){voiceControl.clear();$('voiceConfirm').hidden=true;}
function liveSay(text){
 voiceStatus(text);
 liveSend({type:'response.create',response:{tool_choice:'none',instructions:'Say exactly this and nothing else: '+text}});
}
function showVoiceQuestion(spoken=true){
 $('voiceConfirm').hidden=false;voiceStatus('Do you want me to stop the task?');
 if(spoken)liveSay('Do you want me to stop the task?');
}
async function stopExactVoiceTask(task){
 clearVoiceQuestion();
 if(!task)return;
 try{
  const chat=await api('chats/'+task.cid);
  const target=chat.runs.find(r=>r.id===task.rid);
  if(!target||!['running','waiting'].includes(target.status)){liveSay('That task is no longer running.');return;}
  await api('runs/'+task.rid+'/stop',{});
  liveSay('The task has stopped. Completed changes remain.');last='';await refresh();
 }catch(e){voiceStatus('I could not confirm the task stopped. Check the task panel.');error(e);}
}
$('voiceConfirmYes').onclick=()=>stopExactVoiceTask(voiceControl.confirm(voiceTaskIdentity()));
$('voiceConfirmNo').onclick=()=>{clearVoiceQuestion();voiceStatus('Okay, the task will keep running.');};
async function liveTranscript(text,utteranceStarted=Date.now()){
 let task=voiceTaskIdentity();
 if(task){
  try{
   const chat=await api('chats/'+task.cid);
   const target=chat.runs.find(r=>r.id===task.rid);
   if(!target||!['running','waiting','cancelling'].includes(target.status)){
    if(liveWork?.rid===task.rid)liveWork=null;
    clearVoiceQuestion();liveSay('That task has finished. Please say your next request.');return;
   }
  }catch(e){voiceStatus('I cannot verify the task state. It has not been cancelled.');return;}
 }
 const decision=voiceControl.hear(text,task,Date.now(),utteranceStarted);
 if(decision.kind==='conversation'){clearVoiceQuestion();liveSend({type:'response.create'});return;}
 if(decision.kind==='ask'){showVoiceQuestion();return;}
 $('voiceConfirm').hidden=true;
 if(decision.kind==='stop'){await stopExactVoiceTask(decision.task);return;}
 if(decision.kind==='continue'){liveSay('Okay, I will keep working.');return;}
 liveSay('The current task is still running. I have not started a new request or changed it. Say cancel the task if you want it stopped.');
}
function endLive(){
 clearVoiceQuestion();liveResponding=false;liveSeenInputs.clear();liveSeenCalls.clear();liveSpeechTurns.clear();
 clearTimeout(liveTimer);liveStream?.getTracks().forEach(t=>t.stop());liveStream=null;
 liveChannel?.close();liveChannel=null;const peer=livePeer;livePeer=null;peer?.close();
 $('liveAudio').pause();$('liveAudio').srcObject=null;$('liveAudio').hidden=true;
 $('liveVoice').textContent='◉ Live conversation';$('liveMute').hidden=true;liveStarting=false;
 stopMeter();document.body.classList.remove('live');setOrb(run?'working':'idle');
 voiceStatus('Live conversation ended. Microphone off.');
}
function liveSend(value){if(liveChannel?.readyState==='open')liveChannel.send(JSON.stringify(value));}
async function liveTask(e){
 const channel=liveChannel;let result;let ownedTask=null,ownedSubmission=false;
 try{
  if(e.name&&e.name!=='pi_task')throw Error('Unsupported live tool.');
  if(liveSubmitting||voiceTaskIdentity())throw Error('A task is already running. No new request was started.');
  liveSubmitting=true;ownedSubmission=true;
  const args=JSON.parse(e.arguments);voiceStatus('Working on your Pi…');setOrb('working');
  if(!current)current=(await api('chats',{})).id;
  const cid=current;const task=await api('chats/'+cid+'/send',{text:args.request});
  ownedTask={rid:task.run,cid};liveWork=ownedTask;liveSubmitting=false;clearVoiceQuestion();
  while(liveWork===ownedTask){
   await new Promise(r=>setTimeout(r,1000));
   const d=await api('chats/'+cid);const r=d.runs.find(x=>x.id===task.run);
   if(r&&!['running','waiting','cancelling'].includes(r.status)){
    result=['complete','partial'].includes(r.status)?(r.answer||'The task finished. Check the task panel for the result.'):r.events.filter(x=>x.kind==='error').map(x=>x.text).join(' ')||('Task '+r.status);
    if(liveWork===ownedTask){liveWork=null;clearVoiceQuestion();}break;
   }
  }
  if(!result)result='Task is still running. Check the task panel for its outcome.';
  last='';await refresh();
 }catch(err){result='Task request could not be confirmed: '+err.message;}
 finally{if(ownedSubmission)liveSubmitting=false;}
 if(channel!==liveChannel)return;
 setOrb('listening');
 liveSend({type:'conversation.item.create',item:{type:'function_call_output',call_id:e.call_id,output:String(result).slice(0,16000)}});
 liveSend({type:'response.create'});
}
$('liveMute').onclick=()=>{
 if(!liveStream)return;const muted=liveStream.getAudioTracks()[0].enabled;
 liveStream.getAudioTracks().forEach(t=>t.enabled=!muted);
 $('liveMute').textContent=muted?'Unmute mic':'Mute mic';voiceStatus(muted?'Microphone muted.':'Listening — speak naturally.');
};
$('liveVoice').onclick=async()=>{
 if(livePeer||liveStarting){endLive();return;}
 if(!state?.configured){error(Error('Connect OpenAI first.'));return;}
 if(voiceBusy){error(Error('Finish the current recording or voice request first.'));return;}
 liveStarting=true;stopVoice();$('error').textContent='';$('liveVoice').textContent='End conversation';
 document.body.classList.add('live');setOrb('connecting');voiceStatus('Connecting live voice… Audio is sent to OpenAI.');
 try{
  const peer=new RTCPeerConnection();livePeer=peer;
  const stream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true}});
  if(livePeer!==peer){stream.getTracks().forEach(t=>t.stop());return;}
  liveStream=stream;stream.getAudioTracks().forEach(t=>peer.addTrack(t,stream));startMeter(stream);
  peer.ontrack=e=>{$('liveAudio').srcObject=e.streams[0]||new MediaStream([e.track]);$('liveAudio').hidden=false;$('liveAudio').play().catch(()=>voiceStatus('Tap play to hear live replies.'));};
  peer.onconnectionstatechange=()=>{if(livePeer!==peer)return;if(['failed','disconnected','closed'].includes(peer.connectionState)){endLive();error(Error('Live voice disconnected. Start again.'));}};
  const channel=peer.createDataChannel('oai-events');liveChannel=channel;
  channel.onopen=()=>{voiceStatus('Listening — speak naturally. You can interrupt replies.');setOrb('listening');$('liveMute').hidden=false;$('liveMute').textContent='Mute mic';};
  channel.onmessage=({data})=>{
   let e;try{e=JSON.parse(data);}catch{return;}
   if(e.type==='session.created')liveSend({type:'response.create',response:{output_modalities:['audio'],instructions:'Say briefly: Hello, I am Piko. What would you like to do on your Pi?'}});
   if(e.type==='error'){error(Error(e.error?.message||'Live voice error'));voiceStatus('Live voice error — see the message below.');setOrb('error');}
   if(e.type==='response.done'&&e.response?.status==='failed'){const detail=e.response.status_details?.error;error(Error(detail?.message||'OpenAI could not generate a live reply. Check API credits and try again.'));voiceStatus('Live reply failed.');setOrb('error');}
   if(e.type==='conversation.item.input_audio_transcription.failed')error(Error('Could not hear your words. Check your selected microphone.'));
   if(e.type==='response.done')liveResponding=false;
   if(e.type==='response.created'){liveResponding=true;voiceStatus('Preparing spoken reply…');setOrb('connecting');}
   if(e.type==='output_audio_buffer.started'){voiceStatus('Speaking… You can interrupt.');setOrb('speaking');}
   if(e.type==='output_audio_buffer.stopped'||e.type==='output_audio_buffer.cleared')setOrb('listening');
   if(e.type==='input_audio_buffer.speech_started'){
    if(e.item_id)liveSpeechTurns.set(e.item_id,Date.now());
    if(liveResponding)liveSend({type:'response.cancel'});
    liveSend({type:'output_audio_buffer.clear'});
    voiceControl.interrupt(voiceTaskIdentity());
    voiceStatus('Hearing your voice…');setOrb('hearing');
   }
   if(e.type==='input_audio_buffer.speech_stopped')setOrb('listening');
   if(e.type==='response.output_audio_transcript.done'){$('liveTranscript').textContent='Piko: '+e.transcript;voiceStatus('Listening — speak naturally.');}
   if(e.type==='conversation.item.input_audio_transcription.completed'){
    if(!e.item_id||liveSeenInputs.has(e.item_id))return;
    liveSeenInputs.add(e.item_id);
    $('liveTranscript').textContent='You: '+e.transcript;
    liveTranscript(e.transcript,liveSpeechTurns.get(e.item_id)??0).catch(error);
    liveSpeechTurns.delete(e.item_id);
   }
   if(e.type==='response.function_call_arguments.done'&&e.call_id&&!liveSeenCalls.has(e.call_id)){liveSeenCalls.add(e.call_id);liveTask(e);}
  };
  await peer.setLocalDescription(await peer.createOffer());
  const r=await voiceResponse('live',JSON.stringify({sdp:peer.localDescription.sdp}));
  const answer=await r.text();if(livePeer!==peer)return;
  await peer.setRemoteDescription({type:'answer',sdp:answer});liveStarting=false;
  liveTimer=setTimeout(endLive,30*60*1000);
 }catch(e){endLive();error(Error(e.name==='NotAllowedError'?'Allow microphone access to start a live conversation.':e.message));}
};
window.addEventListener('pagehide',endLive);

/* ---------- devices: constellation map + cards + rail count ---------- */
let devicesLoading=false;
function paintMap(devices){
 const map=$('deviceMap');map.replaceChildren();
 const W=400,H=250,cx=W/2,cy=H/2,rx=150,ry=90;
 const pi=devices.find(d=>d.is_pi)||{name:'Pi host',online:false};
 const peers=devices.filter(d=>!d.is_pi);
 peers.forEach((d,i)=>{
  const a=-Math.PI/2+(i/peers.length)*Math.PI*2;const x=cx+Math.cos(a)*rx,y=cy+Math.sin(a)*ry;
  map.append(svg('line',{class:'wire'+(d.online?' online':''),x1:cx,y1:cy,x2:x.toFixed(1),y2:y.toFixed(1)}));
  const g=svg('g',{class:'node'+(d.online?' online':'')});
  g.append(svg('circle',{class:'halo',cx:x.toFixed(1),cy:y.toFixed(1),r:7}),svg('circle',{class:'nodeDot',cx:x.toFixed(1),cy:y.toFixed(1),r:4.5}));
  const t=svg('text',{x:x.toFixed(1),y:(y+(Math.sin(a)>=0?18:-11)).toFixed(1),'text-anchor':'middle'});t.textContent=d.name.slice(0,20);g.append(t);
  map.append(g);
 });
 const g=svg('g',{class:'node pi'+(pi.online?' online':'')});
 g.append(svg('circle',{class:'halo',cx,cy,r:11}),svg('circle',{class:'nodeDot',cx,cy,r:8}));
 const t=svg('text',{x:cx,y:cy+26,'text-anchor':'middle'});t.textContent=pi.name;g.append(t);map.append(g);
 if(!peers.length){const t=svg('text',{x:cx,y:cy-30,'text-anchor':'middle'});t.textContent='No peers reported';map.append(t);}
}
async function refreshDevices(){
 if(devicesLoading||$('shell').hidden)return;devicesLoading=true;
 try{
  const d=await api('devices');const online=d.devices.filter(x=>x.online).length;
  $('vDevices').textContent=online+' / '+d.devices.length;
  if(!$('devices').open)return;
  $('devicesError').textContent='';
  $('deviceSummary').textContent=online+' online · '+d.devices.length+' devices';
  paintMap(d.devices);
  $('deviceList').replaceChildren();
  for(const device of d.devices){
   const card=node('section',undefined,'deviceCard');const head=node('div',undefined,'metricsHeader');
   head.append(node('strong',device.name+(device.is_pi?' · This Pi':'')),node('span',device.online?'Online':'Offline','badge '+(device.online?'deviceOnline':'deviceOffline')));card.append(head);
   card.append(node('p',device.os+' · '+(device.ip||'No address')));
   if(device.hostname)card.append(node('small',device.hostname));
   const seen=device.online?'Connected to Tailscale':device.last_seen?'Last seen '+new Date(device.last_seen).toLocaleString():'Last seen unavailable';card.append(node('p',seen));
   $('deviceList').append(card);
  }
  if(!d.devices.length)$('deviceList').append(node('p','No devices were reported.'));
  $('devicesChecked').textContent=(d.connected?'':'Pi is disconnected from Tailscale. ')+'Checked '+new Date(d.checked_at*1000).toLocaleTimeString();
 }catch(e){
  if($('devices').open){$('devicesError').textContent=e.message;$('devicesChecked').textContent='Could not refresh — displayed status may be out of date.';}
  else $('vDevices').textContent='—';
 }finally{devicesLoading=false;}
}
$('devicesOpen').onclick=()=>{closeRail();$('devices').showModal();refreshDevices();};
$('devicesClose').onclick=()=>$('devices').close();
setInterval(()=>{if($('devices').open)refreshDevices();},10000);
setInterval(()=>{if(railVisible()&&!$('devices').open)refreshDevices();},60000);

