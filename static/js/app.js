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


function gapChip(active){
  return active ? '<span class="tag chip-gap" id="gap-chip">跳空</span>' : '<span class="tag chip-ok" id="gap-chip">无跳空</span>';
}

async function renderResume(){
  const j = await api('/api/resume/panels');
  const gapActive = !!(j.detail && j.detail.chip === '跳空');
  const sameSrc = j.detail.same_source;
  const ledger = (j.ledger||[]).map(r=>{
    const gap = r.status==='跳空';
    return `<article class="card ${gap?'card-gap':''}">
      <h3>片 ${r.slice_no} ${gap?'<span class="tag chip-gap">跳空</span>':''}</h3>
      <span class="tag">${r.status}</span>
    </article>`;
  }).join('') || '<p class="muted">空</p>';
  const events = (j.events||[]).map(e=>`<li><span class="muted">${e.created_at}</span> ${e.event} ${e.from_offset??''}→${e.to_offset??''} <span class="muted">${e.detail||''}</span></li>`).join('') || '<li class="muted">暂无留痕</li>';
  view.innerHTML = `
  <div class="panel-grid">
    <section class="card panel" id="panel-ledger">
      <h3>跳空台账 ${gapChip(gapActive)}</h3>
      <div class="wall ledger-wall">${ledger}</div>
      <div class="row-actions">
        <button id="p" type="button">暂停到片3</button>
        <button class="primary" id="g" type="button">续灌</button>
      </div>
    </section>
    <section class="card panel" id="panel-detail">
      <h3>偏移详情 ${gapChip(gapActive)}</h3>
      <p>现行偏移 <b>${j.detail.live}</b></p>
      <p>可灌进度 <b>${j.detail.gate}</b></p>
      <p>同源校验：${sameSrc ? '<span class="ok-text">双位同源放行</span>' : '<span class="err">双位不一致已拦截</span>'}</p>
      <p class="muted">水线复验：${j.recheck.continuous ? `序号连续 ${(j.recheck.range||[]).join('~')}` : `发现空档 ${(j.recheck.missing||[]).join(',')}`}</p>
    </section>
    <section class="card panel" id="panel-strategy">
      <h3>续跑策略 ${gapChip(gapActive)}</h3>
      <span class="tag">${j.strategy.btn}</span>
      <p>${j.strategy.fate}</p>
      <p class="muted">打开即回滚半截重写，旁路位点凭证原子切换，旧偏移被挡不再占灌。</p>
    </section>
    <section class="card panel" id="panel-water">
      <h3>覆盖水线 ${gapChip(gapActive)}</h3>
      <p>重写片 <b>${j.water.rewritten.length}</b>（成功片不得被重写）</p>
      <p>跳空片 <b>${j.water.skipped.length}</b>：${(j.water.skipped||[]).map(s=>s.slice_no).join('、')||'无'}</p>
      <p>水线偏移 <b>${j.water.live_offset}</b> / 可灌 <b>${j.water.gate_offset}</b></p>
      <p class="muted">切片与水线复验${j.water.recheck.continuous?'通过，未跳空':'不通过：'+(j.water.recheck.missing||[]).join(',')}</p>
    </section>
  </div>
  <section class="card panel" style="margin-top:0.85rem">
    <h3>编排留痕</h3>
    <ul class="events">${events}</ul>
  </section>`;
  document.getElementById('p').onclick = async ()=>{
    try{ await api('/api/resume/pause',{method:'POST',body:'{"slice_no":3}'}); setMsg('已暂停到片3'); await renderResume(); }
    catch(e){ setMsg(e.message,true); }
  };
  document.getElementById('g').onclick = async ()=>{
    try{
      const r = await api('/api/resume/go',{method:'POST',body:'{}'});
      setMsg(`续灌完成：位点 ${r.from_offset} → ${r.offset}，跳空片 ${(r.gap_slices||[]).join('、')} 待补，旧成功片未重写`);
      await renderResume();
    }catch(e){ setMsg(e.message,true); }
  };
}

async function show(tab){
  setMsg(''); await loadWindows();
  await renderResume();
  const target = document.getElementById('panel-' + tab);
  if (target) target.scrollIntoView({behavior:'smooth', block:'nearest'});
}
document.querySelectorAll('.chips button').forEach(btn=>{
  btn.onclick=()=>{ document.querySelectorAll('.chips button').forEach(b=>b.classList.remove('on')); btn.classList.add('on'); show(btn.dataset.tab); };
});
document.getElementById('btn-logout').onclick = async ()=>{ await api('/api/logout',{method:'POST',body:'{}'}); location.href='/login'; };
show('ledger');
