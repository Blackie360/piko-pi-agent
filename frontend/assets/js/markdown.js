'use strict';
/* Deliberately limited Markdown. Untrusted input is always text, never innerHTML.
   Only bare <details>/<summary> wrappers are recognized; attributes and arbitrary
   HTML remain literal text. No scripts, images, event handlers or remote embeds. */
const PikoMarkdown=(()=>{
 const el=(tag,text)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n;};
 function inline(target,text){
  const re=/`([^`\n]+)`|\*\*([^*\n]+)\*\*|\[([^\]\n]+)\]\(([^\s)]+)\)|(https?:\/\/[^\s<>"')]+)/g;
  let start=0,m;
  while((m=re.exec(text))){
   target.append(document.createTextNode(text.slice(start,m.index)));
   if(m[1]!==undefined)target.append(el('code',m[1]));
   else if(m[2]!==undefined)target.append(el('strong',m[2]));
   else{
    const label=m[3]||m[5],href=m[4]||m[5];let url;
    try{url=new URL(href);}catch{}
    if(url&&['http:','https:'].includes(url.protocol)){
     const a=el('a',label);a.href=url.href;a.target='_blank';a.rel='noopener noreferrer';target.append(a);
    }else target.append(document.createTextNode(m[0]));
   }
   start=re.lastIndex;
  }
  target.append(document.createTextNode(text.slice(start)));
 }
 function cells(line){
  const result=[];let text='',code=false;
  line=line.trim().replace(/^\|/,'').replace(/\|$/,'');
  for(let i=0;i<line.length;i++){
   const c=line[i];
   if(c==='\\'&&line[i+1]==='|'){text+='|';i++;continue;}
   if(c==='`')code=!code;
   if(c==='|'&&!code){result.push(text.trim());text='';}else text+=c;
  }
  result.push(text.trim());return result;
 }
 const divider=line=>cells(line).every(c=>/^:?-{3,}:?$/.test(c));
 function blocks(parent,lines,depth=0){
  for(let i=0;i<lines.length;){
   const line=lines[i],trim=line.trim();
   if(!trim){i++;continue;}
   const fence=trim.match(/^(`{3,}|~{3,})(.*)$/);
   if(fence){
    const body=[];i++;
    while(i<lines.length&&!new RegExp('^'+fence[1][0]+'{'+fence[1].length+',}\\s*$').test(lines[i].trim()))body.push(lines[i++]);
    if(i<lines.length)i++;
    const pre=el('pre',body.join('\n'));pre.className='code';parent.append(pre);continue;
   }
   if(trim==='<details>'&&depth<8){
    let end=i+1,nesting=1;
    while(end<lines.length){if(lines[end].trim()==='<details>')nesting++;if(lines[end].trim()==='</details>'&&!--nesting)break;end++;}
    if(end<lines.length){
     const details=el('details'),content=lines.slice(i+1,end);let title='Details';
     const first=content.findIndex(l=>l.trim());
     const summary=first>=0&&content[first].trim().match(/^<summary>(.*?)<\/summary>$/);
     if(summary){title=summary[1];content.splice(first,1);}
     const heading=el('summary');inline(heading,title);details.append(heading);blocks(details,content,depth+1);parent.append(details);i=end+1;continue;
    }
   }
   const heading=line.match(/^ {0,3}(#{1,6})\s+(.+?)\s*#*$/);
   if(heading){const h=el('h'+heading[1].length);inline(h,heading[2]);parent.append(h);i++;continue;}
   if(i+1<lines.length&&line.includes('|')&&divider(lines[i+1])){
    const wrap=el('div'),table=el('table'),head=el('thead'),row=el('tr'),body=el('tbody');
    wrap.className='tableScroll';wrap.tabIndex=0;wrap.setAttribute('role','region');wrap.setAttribute('aria-label','Scrollable table');
    for(const value of cells(line)){const th=el('th');th.scope='col';inline(th,value);row.append(th);}
    head.append(row);table.append(head,body);i+=2;
    while(i<lines.length&&lines[i].trim()&&lines[i].includes('|')){const tr=el('tr');for(const value of cells(lines[i++])){const td=el('td');inline(td,value);tr.append(td);}body.append(tr);}
    wrap.append(table);parent.append(wrap);continue;
   }
   const list=line.match(/^\s*(?:([-+*])|(\d+)[.)])\s+(.+)$/);
   if(list){
    const ordered=!!list[2],container=el(ordered?'ol':'ul');if(ordered)container.start=Number(list[2]);
    while(i<lines.length){const item=lines[i].match(/^\s*(?:([-+*])|(\d+)[.)])\s+(.+)$/);if(!item||!!item[2]!==ordered)break;const li=el('li');inline(li,item[3]);container.append(li);i++;}
    parent.append(container);continue;
   }
   if(/^>\s?/.test(line)){const quote=el('blockquote'),body=[];while(i<lines.length&&/^>\s?/.test(lines[i]))body.push(lines[i++].replace(/^>\s?/,''));if(depth<8)blocks(quote,body,depth+1);else quote.textContent=body.join('\n');parent.append(quote);continue;}
   const paragraph=el('p');if(trim.startsWith('<'))paragraph.textContent=line;else inline(paragraph,line);parent.append(paragraph);i++;
  }
 }
 return {render(text){const box=el('div');box.className='body';blocks(box,String(text).replace(/\r\n?/g,'\n').split('\n'));return box;}};
})();

