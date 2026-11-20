const msg = document.getElementById('msg');
const view = document.getElementById('view');
const rejectChip = document.getElementById('reject-chip');
let windows = [];

async function api(path, opts={}) {
  const r = await fetch(path, { headers:{'Content-Type':'application/json'}, ...opts });
  const j = await r.json().catch(()=>({}));
  if (!r.ok) throw new Error(j.error || ('HTTP '+r.status));
  return j;
}
function setMsg(t, err=false){ msg.textContent=t||''; msg.className = err?'err':'muted'; }
function esc(s){ return String(s==null?'':s).replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
async function loadWindows(){ windows = (await api('/api/windows')).items || []; refreshRejectChip(); }

// 拒签芯片：只要有落库片缺证，页顶就常亮，值班不用翻台账也知道有片过不了闸。
function refreshRejectChip(){
  const bad = windows.filter(w => (w.uncertified_count||0) > 0);
  if (!bad.length){ rejectChip.className = 'reject-chip hidden'; rejectChip.textContent=''; return; }
  const total = bad.reduce((n,w)=>n+(w.uncertified_count||0),0);
  rejectChip.className = 'reject-chip';
  rejectChip.textContent = `拒签 · ${total} 片缺证`;
  rejectChip.title = bad.map(w=>`窗口#${w.id} ${w.table_name} 缺证${w.uncertified_count}片`).join('；');
}

function renderWindows(){
  view.innerHTML = `<div class="wall">${windows.map(w=>`
    <article class="card">
      <h3>${esc(w.table_name)}</h3>
      <p class="muted">${w.start_date} → ${w.end_date}</p>
      <span class="tag">${w.status}</span>
      ${(w.uncertified_count||0)>0?'<span class="tag bad">缺证拒签</span>':''}
      <p class="muted">切片 ${w.slice_count} · 已落库 ${w.loaded_count} · 有证 ${w.certified_count||0} · 缺证 ${w.uncertified_count||0}</p>
    </article>`).join('')}</div>`;
}

function winOptions(){ return windows.map(w=>`<option value="${w.id}">#${w.id} ${esc(w.table_name)}</option>`).join(''); }

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
        <p class="muted slice-key">片键 ${s.slice_key || '—'}</p>
        <span class="tag">${s.status}</span>
        <span class="tag ${s.gate_status==='已出证'?'ok':(s.gate_status==='缺证'?'bad':'')}">${s.gate_status}</span>
        <p class="muted">行数 ${s.row_count} · 凭证 ${s.credential_no || '无'}</p>
        <button data-id="${s.id}" class="primary" type="button" ${s.status==='已落库' && s.gate_status==='已出证' ? 'disabled':''}>${s.status==='已落库' && s.gate_status==='缺证' ? '重跑补证' : '重放'}</button>
        <button data-detail="${s.id}" type="button">看详情</button>
      </article>`).join('') || '<p class="muted">先生成切片计划</p>';
    document.querySelectorAll('#list button[data-id]').forEach(btn=>{
      btn.onclick = async ()=>{
        try{
          const j = await api(`/api/slices/${btn.dataset.id}/replay`, {method:'POST', body:'{}'});
          setMsg(`已落库 ${j.slice.slice_date} · ${j.slice.row_count} 行 · 当场出证 ${j.slice.credential_no}`);
          await draw(); await loadWindows();
        }catch(e){ setMsg(e.message,true); }
      };
    });
    document.querySelectorAll('#list button[data-detail]').forEach(btn=>{
      btn.onclick = ()=> showSliceDetail(btn.dataset.detail);
    });
  };
  document.getElementById('wid').onchange = draw;
  await draw();
}

async function showSliceDetail(id){
  try{
    const d = await api(`/api/slices/${id}`);
    document.querySelector('.drawer')?.remove();
    view.insertAdjacentHTML('beforeend', `<div class="drawer card">
      <h3>片键 ${esc(d.slice.slice_key)}</h3>
      <p class="muted">窗口 #${d.slice.window_id} ${esc(d.slice.table_name)} · ${d.slice.slice_date}</p>
      <p>状态 ${d.slice.status} · 闸口 ${d.slice.gate_status} · 行数 ${d.slice.row_count}</p>
      <p>凭证号 ${d.slice.credential_no || '无'}</p>
      ${d.credential && d.credential.invalid_reason ? `<p class="err">凭证 ${d.credential.credential_no} 摘要核不上：${d.credential.invalid_reason}</p>` : ''}
      ${d.credential && !d.credential.invalid_reason ? `<p class="muted">出证人 ${d.credential.operator} · 出证时间 ${d.credential.issued_at}</p>` : ''}
      ${!d.slice.credential_no ? '<p class="err">该片没有有效出证凭证，闸口会拒签</p>' : ''}
      <h4>片内留痕</h4>
      <div class="trail">${d.ledger.map(l=>`<p class="muted">${l.created_at} · ${esc(l.action)} · ${esc(l.outcome)} · ${esc(l.operator)}<br/>${esc(l.detail)}</p>`).join('') || '<p class="muted">暂无记录</p>'}</div>
      <button type="button" id="close-drawer">收起</button>
    </div>`);
    document.getElementById('close-drawer').onclick = ()=> document.querySelector('.drawer').remove();
  }catch(e){ setMsg(e.message,true); }
}

async function renderGate(){
  await loadWindows();
  view.innerHTML = `<div class="card">
    <label>窗口<select id="wid">${winOptions()}</select></label>
    <p class="muted">出证闸口只认落库当场写下的凭证；缺证一律拒签，不现场补证。</p>
    <div id="list" class="wall" style="margin-top:0.8rem"></div>
  </div>`;
  const draw = async ()=>{
    const id = document.getElementById('wid').value;
    const items = (await api(`/api/windows/${id}/slices`)).items || [];
    document.getElementById('list').innerHTML = items.map(s=>`
      <article class="card">
        <h3>${s.slice_date}</h3>
        <p class="muted slice-key">片键 ${s.slice_key || '—'}</p>
        <span class="tag ${s.gate_status==='已出证'?'ok':(s.gate_status==='缺证'?'bad':'')}">${s.gate_status}</span>
        <p class="muted">凭证 ${s.credential_no || '无'}</p>
        <button data-gate="${s.id}" class="primary" type="button">点出证</button>
      </article>`).join('') || '<p class="muted">先生成切片计划</p>';
    document.querySelectorAll('#list button[data-gate]').forEach(btn=>{
      btn.onclick = async ()=>{
        try{
          const j = await api(`/api/slices/${btn.dataset.gate}/gate`, {method:'POST', body:'{}'});
          setMsg(`闸口放行 · 凭证号 ${j.credential.credential_no} · 片键 ${j.credential.slice_key}`);
        }catch(e){
          setMsg(`闸口拒签：${e.message}`, true);
          await loadWindows();
        }
      };
    });
  };
  document.getElementById('wid').onchange = draw;
  await draw();
}

async function renderLedger(){
  view.innerHTML = `<div class="card">
    <label>窗口<select id="wid"><option value="">全部窗口</option>${winOptions()}</select>
      <button type="button" id="go" style="margin-top:0.5rem">查台账</button></label>
    <div id="trail" class="trail"></div>
  </div>`;
  const draw = async ()=>{
    const wid = document.getElementById('wid').value;
    const items = (await api('/api/ledger' + (wid?`?window_id=${wid}`:''))).items || [];
    document.getElementById('trail').innerHTML = items.map(l=>`
      <p class="${l.outcome==='拒签'||l.outcome==='失败'?'err':'muted'}">
        ${l.created_at} · ${esc(l.action)} · <b>${esc(l.outcome)}</b> · ${esc(l.operator)}<br/>
        凭证 ${esc(l.credential_no || '无')} · ${esc(l.detail)}
      </p>`).join('') || '<p class="muted">暂无台账记录</p>';
  };
  document.getElementById('wid').onchange = draw;
  document.getElementById('go').onclick = draw;
  await draw();
}

async function renderCoverage(){
  view.innerHTML = `<div class="card">
    <label>窗口<select id="wid">${winOptions()}</select></label>
    <button class="primary" id="go" type="button">跑覆盖对账</button>
    <button type="button" id="verify">目录再验</button>
    <div id="dir" class="wall" style="margin-top:0.8rem"></div>
    <h4>历史对账</h4>
    <div id="hist" class="wall"></div>
  </div>`;
  const drawDir = async ()=>{
    const wid = document.getElementById('wid').value;
    const rows = (await api('/api/coverage-directory' + (wid?`?window_id=${wid}`:''))).items || [];
    document.getElementById('dir').innerHTML = rows.map(r=>`
      <article class="card">
        <h3>窗口 #${r.window_id} ${esc(r.table_name)}</h3>
        <span class="tag ${r.complete?'ok':'bad'}">${esc(r.verdict)}</span>
        <p class="muted">计划 ${r.planned} · 已落库 ${r.loaded} · 有证 ${r.certified} · 缺证 ${r.uncertified} · 未落库 ${r.missing}</p>
        ${r.uncertified_slices.length ? `<p class="err">无证片：${r.uncertified_slices.map(s=>esc(s.slice_date)).join('、')}</p>` : ''}
      </article>`).join('') || '<p class="muted">暂无覆盖目录</p>';
  };
  const drawHist = async ()=>{
    const items = (await api('/api/coverage-reports')).items || [];
    document.getElementById('hist').innerHTML = items.map(r=>`
      <article class="card"><h3>窗口 #${r.window_id}</h3>
        <span class="tag ${r.uncertified>0?'bad':''}">${esc(r.verdict||'')}</span>
        <p>${esc(r.summary)}</p></article>`).join('') || '<p class="muted">暂无对账记录</p>';
  };
  document.getElementById('wid').onchange = drawDir;
  document.getElementById('go').onclick = async ()=>{
    try{
      const wid = Number(document.getElementById('wid').value);
      const j = await api('/api/coverage-reports', {method:'POST', body: JSON.stringify({window_id: wid})});
      setMsg(j.report.summary, j.report.uncertified>0);
      await Promise.all([drawHist(), drawDir(), loadWindows()]);
    }catch(e){ setMsg(e.message,true); }
  };
  document.getElementById('verify').onclick = async ()=>{
    try{
      const wid = Number(document.getElementById('wid').value);
      const j = await api('/api/coverage-directory', {method:'POST', body: JSON.stringify({window_id: wid})});
      setMsg(`目录再验放行：${j.detail}`);
      await drawDir();
    }catch(e){ setMsg(`目录再验拒签：${e.message}`, true); await drawDir(); }
  };
  await Promise.all([drawDir(), drawHist()]);
}

async function show(tab){
  setMsg(''); await loadWindows();
  if (tab==='windows') renderWindows();
  if (tab==='plan') renderPlan();
  if (tab==='replay') await renderReplay();
  if (tab==='gate') await renderGate();
  if (tab==='ledger') await renderLedger();
  if (tab==='coverage') await renderCoverage();
}
document.querySelectorAll('.chips button').forEach(btn=>{
  btn.onclick=()=>{ document.querySelectorAll('.chips button').forEach(b=>b.classList.remove('on')); btn.classList.add('on'); show(btn.dataset.tab); };
});
document.getElementById('btn-logout').onclick = async ()=>{ await api('/api/logout',{method:'POST',body:'{}'}); location.href='/login'; };
show('windows');
