'use strict';
// Speech interruption is separate from cancellation. No model tool can confirm this gate.
class VoiceTaskControl {
 constructor(){this.pending=null;}
 clear(){this.pending=null;}
 same(a,b){return !!a&&!!b&&a.rid===b.rid&&a.cid===b.cid;}
 interrupt(task,now=Date.now(),needsTranscript=true){
  if(!task){this.clear();return false;}
  if(this.pending&&this.same(this.pending.task,task)&&now<this.pending.expires)return false;
  this.pending={task:{...task},expires:now+30000,asked:!needsTranscript,promptedAt:needsTranscript?Infinity:now};return true;
 }
 hear(text,task,now=Date.now(),utteranceStarted=now){
  const words=text.toLowerCase().trim().replace(/[.!?]+$/,'').replace(/\s+/g,' ');
  const explicit=/^(please )?(stop|cancel)( the| my| this)? task( please)?$/.test(words);
  if(!task){this.clear();return {kind:'conversation'};}
  if(explicit){this.clear();return {kind:'stop',task:{...task}};}
  const p=this.pending;
  if(!p||!this.same(p.task,task)||now>=p.expires){this.clear();return {kind:'busy'};}
  if(!p.asked){p.asked=true;p.promptedAt=now;return {kind:'ask'};}
  if(utteranceStarted<p.promptedAt){this.clear();return {kind:'busy'};}
  if(/^(yes|yes please|yes stop it|yes cancel it|stop it|cancel it)$/.test(words)){
   this.clear();return {kind:'stop',task:p.task};
  }
  if(/^(no|no thanks|no thank you|keep going|continue|don't stop|do not stop)$/.test(words)){
   this.clear();return {kind:'continue'};
  }
  // Ambiguous answers and fresh requests never cancel; close this gate to prevent a later stray yes.
  this.clear();return {kind:'busy'};
 }
 confirm(task,now=Date.now()){
  const p=this.pending;
  if(!p||!p.asked||!this.same(p.task,task)||now>=p.expires){this.clear();return null;}
  this.clear();return p.task;
 }
}
if(typeof module!=='undefined')module.exports=VoiceTaskControl;
else globalThis.VoiceTaskControl=VoiceTaskControl;

