'use strict';
const results=[];
function check(name,condition){results.push({name,passed:!!condition});}
const sample='## System status\n\n- **Docker:** running\n- No changes made\n\n1. Inspect\n2. Verify\n\n| Service | Status |\n| --- | --- |\n| Pi agent | **Healthy** |\n| Literal pipe | `a|b` |\n\n<details>\n<summary>Complete package list</summary>\n\n```text\npackage-one\npackage-two\n```\n</details>\n\n> Read-only inspection\n\n[Documentation](https://example.com/docs)';
const rich=PikoMarkdown.render(sample);document.querySelector('#messages').append(rich);
check('Headings rendered',rich.querySelector('h2')?.textContent==='System status');
check('Unordered and ordered lists',rich.querySelectorAll('ul li').length===2&&rich.querySelectorAll('ol li').length===2);
check('Table and inline code pipe',rich.querySelectorAll('tbody tr').length===2&&rich.querySelector('tbody tr:last-child td:last-child code')?.textContent==='a|b');
check('Details collapsed with summary',rich.querySelector('details summary')?.textContent==='Complete package list'&&!rich.querySelector('details').open);
check('Code preserved',rich.querySelector('pre').textContent==='package-one\npackage-two');
check('Blockquote rendered',!!rich.querySelector('blockquote'));
check('Safe external link',rich.querySelector('a')?.rel==='noopener noreferrer');
const attacks=['<img src=x onerror=alert(1)>','<script>alert(1)</script>','<svg onload=alert(1)>','<iframe src="https://example.com">','[bad](javascript:alert(1))','[bad](data:text/html,boom)','<details open onclick="alert(1)">\n<summary>X</summary>\n</details>'];
for(const attack of attacks){const node=PikoMarkdown.render(attack);check('HTML/URL inert: '+attack.slice(0,30),!node.querySelector('img,script,svg,iframe,[onclick],[onerror],[onload],a'));}
const code=PikoMarkdown.render('```html\n<details>\n<script>bad()</script>\n</details>\n```');
check('Fenced HTML stays literal',!code.querySelector('details,script')&&code.querySelector('pre').textContent.includes('<script>'));
const nested=PikoMarkdown.render('<details>\n<summary>Outer</summary>\n<details>\n<summary>Inner</summary>\nHello\n</details>\n</details>');
check('Nested details balanced',nested.querySelectorAll('details').length===2);
const long='sudo -n inspect /fixture/'+ 'long-directory-name'.repeat(35);
document.querySelector('.eventText').textContent='Command: '+long;
document.querySelector('.event.approval pre').textContent=long;
requestAnimationFrame(()=>{
 const target=document.querySelector('.deleteChat').getBoundingClientRect();
 check('Delete target at least 44×44',target.width>=44&&target.height>=44);
 check('No page horizontal overflow',document.documentElement.scrollWidth<=innerWidth);
 check('Long event contained',document.querySelector('.event.info').scrollWidth<=document.querySelector('.event.info').clientWidth);
 check('Long approval contained',document.querySelector('.event.approval').scrollWidth<=document.querySelector('.event.approval').clientWidth);
 document.querySelector('#test-results').textContent=JSON.stringify({passed:results.filter(r=>r.passed).length,total:results.length,failures:results.filter(r=>!r.passed)},null,2);
});

