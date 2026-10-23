'use strict';
const assert=require('node:assert/strict');
const Control=require('./static/voice-control.js');
const task={rid:'task-a',cid:'chat-a'}, other={rid:'task-b',cid:'chat-a'};
let count=0;
function check(name,fn){fn();count++;console.log('PASS '+name);}
check('interruption leaves task running and first yes only prompts',()=>{
 const c=new Control();assert.equal(c.interrupt(task,0),true);
 assert.equal(c.interrupt(task,1),false);
 assert.equal(c.hear('yes',task,2).kind,'ask');
 assert.deepEqual(c.hear('yes please',task,3),{kind:'stop',task});
 assert.equal(c.hear('yes',task,4).kind,'busy');
});
check('negative continues',()=>{const c=new Control();c.interrupt(task,0,false);assert.equal(c.hear('no',task,1).kind,'continue');});
check('new request cannot confirm',()=>{const c=new Control();c.interrupt(task,0,false);assert.equal(c.hear('check memory instead',task,1).kind,'busy');assert.equal(c.hear('yes',task,2).kind,'busy');});
check('ambiguous answers never cancel',()=>{for(const t of ['maybe','okay','yes and delete it','stop speaking','stop']){const c=new Control();c.interrupt(task,0,false);assert.equal(c.hear(t,task,1).kind,'busy');}});
check('stale confirmation cannot cancel new task',()=>{const c=new Control();c.interrupt(task,0,false);assert.equal(c.hear('yes',other,1).kind,'busy');});
check('expired confirmation cannot cancel',()=>{const c=new Control();c.interrupt(task,0,false);assert.equal(c.hear('yes',task,30000).kind,'busy');});
check('clear task intent works without pending gate',()=>{const c=new Control();assert.deepEqual(c.hear('Please cancel the task.',task,0),{kind:'stop',task});});
check('completed task clears gate',()=>{const c=new Control();c.interrupt(task,0,false);assert.equal(c.hear('yes',null,1).kind,'conversation');assert.equal(c.pending,null);});
check('buttons bind exact identity',()=>{const c=new Control();c.interrupt(task,0,false);assert.equal(c.confirm(other,1),null);c.interrupt(task,2,false);assert.deepEqual(c.confirm(task,3),task);assert.equal(c.confirm(task,4),null);});
check('speech begun before question cannot confirm',()=>{const c=new Control();c.interrupt(task,0);assert.equal(c.hear('hello',task,10,0).kind,'ask');assert.equal(c.hear('yes',task,12,5).kind,'busy');});
console.log(count+' voice control tests passed');

// Exercise the production browser handlers with an in-memory DOM/API; no sockets or provider calls.
const vm=require('node:vm'),fs=require('node:fs');
const script=fs.readFileSync('static/app.js','utf8').split('/* ---------- live conversation (WebRTC) ---------- */')[1].split('/* ---------- devices:')[0];
async function wiring(){
 const calls=[],spoken=[],elements=new Map();let status='running',failStop=false;
 const el=id=>{if(!elements.has(id))elements.set(id,{hidden:true,textContent:'',pause(){}});return elements.get(id);};
 const context=vm.createContext({VoiceTaskControl:Control,Date,JSON,Error,console,
  $:el,current:'chat-a',run:{id:'task-a',status:'running'},state:{configured:true},voiceBusy:false,
  window:{addEventListener(){}},document:{body:{classList:{remove(){},add(){}}}},
  clearTimeout(){},setTimeout(){return 0;},stopMeter(){},setOrb(){},stopVoice(){},startMeter(){},
  voiceStatus:t=>spoken.push(t),error(){},refresh:async()=>{},last:'',
  api:async(path)=>{calls.push(path);if(path.startsWith('chats/'))return {runs:[{id:'task-a',status}]};if(failStop)throw Error('fixture failure');status='cancelled';return {ok:true};}});
 vm.runInContext(script,context);
 await vm.runInContext("voiceControl.interrupt(voiceTaskIdentity(),Date.now(),false);liveTranscript('yes')",context);
 assert.equal(calls.filter(p=>p==='runs/task-a/stop').length,1);assert.ok(spoken.includes('The task has stopped. Completed changes remain.'));
 count++;console.log('PASS browser confirmation stops exact task only');
 calls.length=0;status='complete';
 await vm.runInContext("voiceControl.interrupt(voiceTaskIdentity(),Date.now(),false);liveTranscript('yes')",context);
 assert.equal(calls.some(p=>p.includes('/stop')),false);count++;console.log('PASS completed task never calls stop');
 calls.length=0;status='running';
 await vm.runInContext("voiceControl.interrupt(voiceTaskIdentity(),Date.now(),false);liveTranscript('no')",context);
 assert.equal(calls.some(p=>p.includes('/stop')),false);count++;console.log('PASS browser negative leaves task running');
 calls.length=0;failStop=true;
 await vm.runInContext("voiceControl.interrupt(voiceTaskIdentity(),Date.now(),false);liveTranscript('yes')",context);
 assert.ok(spoken.at(-1).includes('could not confirm'));count++;console.log('PASS failed stop is never reported successful');
 calls.length=0;
 await vm.runInContext("voiceControl.interrupt(voiceTaskIdentity(),Date.now(),false);liveTranscript('check another folder instead')",context);
 assert.equal(calls.some(p=>p.includes('/stop')||p.includes('/send')),false);count++;console.log('PASS new spoken request during work neither cancels nor starts work');

 // Start the real event wiring against synthetic WebRTC objects.
 failStop=false;status='running';calls.length=0;
 let channel;
 context.navigator={mediaDevices:{getUserMedia:async()=>({getTracks:()=>[],getAudioTracks:()=>[]})}};
 context.RTCPeerConnection=class{
  addTrack(){} createDataChannel(){channel={readyState:'open',send(){},close(){}};return channel;}
  async createOffer(){return {sdp:'fixture'};} async setLocalDescription(x){this.localDescription=x;}
  async setRemoteDescription(){} close(){}
 };
 context.voiceResponse=async()=>({text:async()=> 'fixture-answer'});
 await el('liveVoice').onclick();
 const emit=e=>channel.onmessage({data:JSON.stringify(e)});
 emit({type:'response.created'});
 emit({type:'input_audio_buffer.speech_started',item_id:'first'});
 assert.equal(calls.some(p=>p.includes('/stop')),false);
 emit({type:'conversation.item.input_audio_transcription.completed',item_id:'first',transcript:'hello'});
 await new Promise(setImmediate);
 assert.ok(spoken.includes('Do you want me to stop the task?'));
 assert.equal(calls.some(p=>p.includes('/stop')),false);
 count++;console.log('PASS speech event stops no task and asks confirmation');
 emit({type:'input_audio_buffer.speech_started',item_id:'confirmation'});
 emit({type:'conversation.item.input_audio_transcription.completed',item_id:'confirmation',transcript:'yes'});
 await new Promise(setImmediate);
 emit({type:'conversation.item.input_audio_transcription.completed',item_id:'confirmation',transcript:'yes'});
 await new Promise(setImmediate);
 assert.equal(calls.filter(p=>p==='runs/task-a/stop').length,1);
 count++;console.log('PASS repeated transcription cannot repeat cancellation');
 console.log(count+' voice tests passed');
}
wiring().catch(e=>{console.error(e);process.exitCode=1;});

