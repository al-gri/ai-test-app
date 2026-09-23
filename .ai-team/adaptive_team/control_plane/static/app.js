'use strict';
let csrf='',view=null,reviewSequence=0,decisionBusy=false;
const $=id=>document.getElementById(id);
// Never render project-controlled text as HTML, including diff/filename content.
const visible=value=>String(value).replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f\u202a-\u202e\u2066-\u2069]/g,c=>'[U+'+c.charCodeAt(0).toString(16).toUpperCase().padStart(4,'0')+']');
const text=(node,value)=>{node.textContent=visible(value);};
const message=value=>text($('message'),value);
async function api(path,method='GET',data){
 const res=await fetch('/api/'+path,{method,credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json','X-UI-Request':'1','X-CSRF-Token':csrf},body:data===undefined?undefined:JSON.stringify(data)});
 const result=await res.json();if(!res.ok){if(res.status===401)show(false);throw Error(result.error||'Request failed');}return result;
}
function show(logged){$('login').hidden=logged;$('workspace').hidden=!logged;$('logout').hidden=!logged;if(!logged){reviewSequence++;csrf='';view=null;$('evidence').textContent='';$('decision-form').hidden=true;}}
function child(parent,tag,value,cls){const n=document.createElement(tag);if(cls)n.className=cls;text(n,value);parent.appendChild(n);return n;}
function graph(data){
 text($('project'),data.project.project);text($('status'),data.project.paused?'Project paused':'Live project state · Refresh to retrieve a consistent snapshot');
 $('metrics').replaceChildren();for(const [label,n] of [['Tasks',data.tasks.length],['Active',data.tasks.filter(t=>t.stage==='active').length],['Human holds',data.tasks.filter(t=>t.held).length],['Accepted',data.tasks.filter(t=>t.status==='accepted').length]]){const d=child($('metrics'),'div',label,'metric');child(d,'strong',n);}
 $('graph').replaceChildren();const parents=new Map(data.tasks.map(t=>[t.id,[]]));for(const e of data.edges)parents.get(e.task_id)?.push(e.parent_id);
 let remaining=[...data.tasks],done=new Set(),level=0;
 while(remaining.length){const wave=remaining.filter(t=>parents.get(t.id).every(p=>done.has(p)));if(!wave.length){child($('graph'),'p','Invalid dependency graph: contact the owner.');break;}
  child($('graph'),'small','WAVE '+(++level));const line=child($('graph'),'div','','wave');for(const t of wave){const b=child(line,'button','','node '+t.stage);child(b,'strong',t.id);child(b,'small',t.stage.replaceAll('_',' '));if(parents.get(t.id).length)child(b,'small','After: '+parents.get(t.id).join(', '));b.onclick=()=>text($('task-detail'),JSON.stringify(t,null,2));done.add(t.id);}remaining=remaining.filter(t=>!done.has(t.id));
 }
}
function billing(data){text($('billing-info'),data.configured?`UTC day ${data.day} · USD · Reservations include unresolved calls from earlier days.`:'Token ledger is not configured. No zero-cost assumption is made.');$('ledger').replaceChildren();const money=x=>x===null?'Not configured':'$'+(x/1e6).toFixed(6);for(const row of data.roles){const tr=child($('ledger'),'tr','');for(const x of [row.role,money(row.daily_limit),money(row.spent),money(row.reserved),row.input_count+' / '+row.output_count])child(tr,'td',x);}}
function requests(items){$('requests').replaceChildren();text($('inbox-count'),items.length);if(!items.length)child($('requests'),'p','No decisions waiting.');for(const item of items){const b=child($('requests'),'button',item.task_id,'request');child(b,'small',item.status+' · '+item.reason);b.onclick=async()=>{if(decisionBusy)return;const sequence=++reviewSequence;view=null;$('decision-form').hidden=true;$('evidence').textContent='';try{const r=await api('review/'+encodeURIComponent(item.id));if(sequence!==reviewSequence)return;view=r;document.querySelectorAll('[data-decision]').forEach(n=>n.disabled=false);const e=r.evidence;text($('review-notice'),e.notice+' · Subject '+e.subject_digest);text($('evidence'),e.kind==='git'?`Base: ${e.base}\nTarget: ${e.target}\n\n${e.diff}`:JSON.stringify(e.document,null,2));$('approve').disabled=!e.complete;$('comment').value='';$('decision-form').hidden=item.status!=='pending';message('');}catch(e){message(e.message);}};}}
async function refresh(){try{const [d,i,b]=await Promise.all([api('dashboard'),api('inbox'),api('ledger')]);graph(d);requests(i);billing(b);message('');}catch(e){message(e.message);}}
$('login-form').onsubmit=async e=>{e.preventDefault();const token=$('token').value;$('token').value='';try{const r=await api('login','POST',{token});csrf=r.csrf;show(true);await refresh();}catch(err){message(err.message);}};
$('logout').onclick=async()=>{try{await api('logout','POST',{});show(false);}catch(e){message(e.message);}};
$('refresh').onclick=refresh;
document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>{for(const id of ['dag','inbox','billing'])$(id).hidden=id!==b.dataset.tab;document.querySelectorAll('[data-tab]').forEach(n=>n.classList.toggle('selected',n===b));});
document.querySelectorAll('[data-decision]').forEach(b=>b.onclick=async()=>{if(!view||decisionBusy)return;decisionBusy=true;const buttons=[...document.querySelectorAll('[data-decision]')];buttons.forEach(n=>n.disabled=true);try{const r=await api('decision','POST',{view_id:view.view_id,decision:b.dataset.decision,comment:$('comment').value});view=null;$('decision-form').hidden=true;await refresh();message('Signed decision recorded: '+r.status);}catch(e){message(e.message);buttons.forEach(n=>n.disabled=false);$('approve').disabled=!view?.evidence.complete;}finally{decisionBusy=false;}});
api('session').then(r=>{csrf=r.csrf;show(true);refresh();}).catch(()=>show(false));
