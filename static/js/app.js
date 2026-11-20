const msg = document.getElementById('msg');
const view = document.getElementById('view');
let windows = [];

async function api(path, opts={}) {
  const r = await fetch(path, { headers:{'Content-Type':'application/json'}, ...opts });
  const j = await r.json().catch(()=>({}));
  if (!r.ok) throw new Error(j.error || ('HTTP '+r.status));
  return j;
}
function setMsg(t, err=false){ msg.textContent=t||''; msg.className = err?'err':'muted'; }
async function loadWindows(){ windows = (await api('/api/windows')).items || []; }

function renderWindows(){
  view.innerHTML = `<div class="wall">${windows.map(w=>`
    <article class="card">
      <h3>${w.table_name}</h3>
      <p class="muted">${w.start_date} → ${w.end_date}</p>
      <span class="tag">${w.status}</span>
      <p class="muted">切片 ${w.slice_count} · 已落库 ${w.loaded_count}</p>
    </article>`).join('')}</div>`;
}

function winOptions(){ return windows.map(w=>`<option value="${w.id}">#${w.id} ${w.table_name}</option>`).join(''); }

function renderPlan(){
  view.innerHTML = `<div class="card">
    <label>窗口<select id="wid">${winOptions()}</select></label>
    <button class="primary" id="go" type="button">生成切片计划</button>
    <pre id="out" class="muted"></pre>
  </div>`;
  document.getElementById('go').onclick = async ()=>{
    try{
      const wid = document.getElementById('wid').value;
      const j = await api(`/api/windows/${wid}/plan`, {method:'POST', body:'{}'});
      document.getElementById('out').textContent = JSON.stringify(j.slices, null, 2);
      setMsg(`已生成 ${j.slices.length} 片`);
      await loadWindows();
    }catch(e){ setMsg(e.message,true); }
  };
}

async function renderReplay(){
  await loadWindows();
  const wid = windows[0]?.id;
  const slices = wid ? (await api(`/api/windows/${wid}/slices`)).items : [];
  view.innerHTML = `<div class="card">
    <label>窗口<select id="wid">${winOptions()}</select></label>
    <div id="list" class="wall" style="margin-top:0.8rem"></div>
  </div>`;
  const draw = async ()=>{
    const id = document.getElementById('wid').value;
    const items = (await api(`/api/windows/${id}/slices`)).items || [];
    document.getElementById('list').innerHTML = items.map(s=>`
      <article class="card">
        <h3>${s.slice_date}</h3>
        <span class="tag">${s.status}</span>
        <p class="muted">行数 ${s.row_count}</p>
        <button data-id="${s.id}" class="primary" type="button" ${s.status!=='待重放'?'disabled':''}>重放</button>
      </article>`).join('') || '<p class="muted">先生成切片计划</p>';
    document.querySelectorAll('#list button[data-id]').forEach(btn=>{
      btn.onclick = async ()=>{
        try{
          const j = await api(`/api/slices/${btn.dataset.id}/replay`, {method:'POST', body:'{}'});
          setMsg(`已落库 ${j.slice.slice_date} · ${j.slice.row_count} 行`);
          await draw(); await loadWindows();
        }catch(e){ setMsg(e.message,true); }
      };
    });
  };
  document.getElementById('wid').onchange = draw;
  await draw();
}

async function renderCoverage(){
  const items = (await api('/api/coverage-reports')).items || [];
  view.innerHTML = `<div class="card">
    <label>窗口<select id="wid">${winOptions()}</select></label>
    <button class="primary" id="go" type="button">跑覆盖对账</button>
    <div class="wall" style="margin-top:0.8rem">${items.map(r=>`
      <article class="card"><h3>窗口 #${r.window_id}</h3><p>${r.summary}</p></article>`).join('')}</div>
  </div>`;
  document.getElementById('go').onclick = async ()=>{
    try{
      const j = await api('/api/coverage-reports', {method:'POST', body: JSON.stringify({window_id: Number(document.getElementById('wid').value)})});
      setMsg(j.report.summary);
      await renderCoverage();
    }catch(e){ setMsg(e.message,true); }
  };
}


async function render_ledger(){
  const j = await api('/api/resume/panels');
  view.innerHTML = `<div class="wall">${(j.ledger||[]).map(r=>`<article class="card"><h3>片 ${r.slice_no}</h3><span class="tag">${r.status}</span></article>`).join('')||'<p class="muted">空</p>'}
    <article class="card"><button id="p" type="button">暂停到片3</button>
    <button class="primary" id="g" type="button">续灌</button></article></div>`;
  document.getElementById('p').onclick = async ()=>{ try{ await api('/api/resume/pause',{method:'POST',body:'{"slice_no":3}'}); setMsg('已暂停'); await render_ledger(); }catch(e){setMsg(e.message,true);} };
  document.getElementById('g').onclick = async ()=>{ try{ const r=await api('/api/resume/go',{method:'POST',body:'{}'}); setMsg(r.chip); await render_ledger(); }catch(e){setMsg(e.message,true);} };
}
async function render_detail(){ const j=await api('/api/resume/panels'); view.innerHTML=`<div class="card"><p>现行偏移 ${j.detail.live}</p><p>可灌进度 ${j.detail.gate}</p></div>`; }
async function render_strategy(){ const j=await api('/api/resume/panels'); view.innerHTML=`<div class="card"><span class="tag">${j.strategy.btn}</span><p>${j.strategy.fate}</p></div>`; }
async function render_water(){ const j=await api('/api/resume/panels'); view.innerHTML=`<div class="card"><p>重写 ${j.water.rewritten.length}</p><p>跳空 ${j.water.skipped.length}</p></div>`; }

async function show(tab){
  setMsg(''); await loadWindows();
  if (tab==='ledger') await render_ledger();
  if (tab==='detail') await render_detail();
  if (tab==='strategy') await render_strategy();
  if (tab==='water') await render_water();
}
document.querySelectorAll('.chips button').forEach(btn=>{
  btn.onclick=()=>{ document.querySelectorAll('.chips button').forEach(b=>b.classList.remove('on')); btn.classList.add('on'); show(btn.dataset.tab); };
});
document.getElementById('btn-logout').onclick = async ()=>{ await api('/api/logout',{method:'POST',body:'{}'}); location.href='/login'; };
show('ledger');
