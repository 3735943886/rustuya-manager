// =============================================================================
// State & Core Engine
// =============================================================================
const s = {
    ws: null, connected: false, mqtt: false, devices: {}, cloud: {},
    sync: { missing: [], mismatched: [], orphaned: [], synced: [] },
    curr: null, filter: 'all', live: {}, errs: {}, logs: [], wiz: {}
};

const $ = id => document.getElementById(id);
const $$ = sel => document.querySelectorAll(sel);
const update = () => { s.sync = computeSync(); requestAnimationFrame(render); };

// =============================================================================
// UI Components & Rendering
// =============================================================================
const showToast = (msg, level = 'info', dur = 3500) => {
    const c = $('toast-container');
    if (!c) return;
    if (c.children.length >= 5) c.children[0].remove();
    const colors = { success: 'bg-emerald-600', error: 'bg-red-700', info: 'bg-slate-700', warning: 'bg-amber-600' };
    const icons = { success: 'check', error: 'xmark', info: 'info', warning: 'triangle-exclamation' };
    const t = document.createElement('div');
    t.className = `flex items-start gap-3 px-4 py-3 rounded-lg border border-white/10 shadow-2xl text-white text-sm transition-all duration-300 opacity-0 -translate-y-4 max-w-md ${colors[level] || colors.info}`;
    t.innerHTML = `<i class="fa-solid fa-circle-${icons[level] || icons.info} mt-0.5"></i><span>${msg}</span>`;
    c.appendChild(t);
    requestAnimationFrame(() => t.classList.remove('opacity-0', '-translate-y-4'));
    setTimeout(() => { t.classList.add('opacity-0', '-translate-y-4'); setTimeout(() => t.remove(), 300); }, dur);
};

const addLog = (msg, level = 'info') => {
    s.logs.unshift({ ts: new Date().toLocaleTimeString(), msg, level });
    if (s.logs.length > 100) s.logs.pop();
    update();
};

const sendCmd = (action, payload = {}) => {
    if (s.connected) { s.ws.send(JSON.stringify({ action, payload })); addLog(`→ ${action}`, 'info'); }
    else showToast('Disconnected', 'error');
};

const isPrivIP = ip => ip === 'auto' || /^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[0-1])\.)/.test(ip || '');
const hasErr = d => s.errs[d.id] || (d.status !== 'online' && d.status !== true && d.status != 0 && !['subdevice','no parent'].includes(d.status) && /^\d+$/.test(d.status));
const isZigbee = d => !!(d.sub || d.parent || d.parent_id || d.cid || d.node_id);

const computeSync = () => {
    const res = { missing: [], mismatched: [], orphaned: [], synced: [] }, bids = Object.keys(s.devices), cids = Object.keys(s.cloud);
    cids.forEach(id => {
        const c = s.cloud[id], b = s.devices[id], diff = [];
        if (!b) return res.missing.push(c);
        if (c.name !== b.name) diff.push('name');
        if ((c.key || c.local_key) !== (b.key || b.local_key)) diff.push('key');
        diff.length ? res.mismatched.push({ cloud: c, bridge: b, reasons: diff }) : res.synced.push(b);
    });
    bids.forEach(id => { if (!cids.includes(id)) res.orphaned.push(s.devices[id]); });
    return res;
};

// --- Render Logic ---
const renderStatus = () => {
    if($('connection-status')) $('connection-status').innerHTML = s.connected ? `<span class="status-dot status-online"></span><span class="text-slate-300">Connected</span>` : `<span class="status-dot status-offline animate-pulse"></span><span class="text-red-400">Disconnected</span>`;
    if($('mqtt-broker-status')) $('mqtt-broker-status').innerHTML = `<span class="status-dot ${s.mqtt ? 'status-online' : 'status-offline'}"></span><span class="text-slate-400 text-xs">MQTT Broker</span>`;
};

const renderLogs = () => {
    if (!$('log-body')) return;
    const clrs = { info: 'text-slate-300', error: 'text-red-400', success: 'text-emerald-400', warning: 'text-amber-400' };
    $('log-body').innerHTML = s.logs.map(l => `<div class="flex gap-3 text-xs py-1.5 border-b border-slate-700/50"><span class="text-slate-500 font-mono">${l.ts}</span><span class="${clrs[l.level]||clrs.info}">${l.msg}</span></div>`).join('') || `<p class="text-center text-slate-500 py-4">No logs</p>`;
};

const renderStats = () => {
    const total = s.sync.missing.length + s.sync.mismatched.length + s.sync.orphaned.length;
    ['total','cloud','synced','conflicts'].forEach((id, i) => $(`stat-${id}`) && ($(`stat-${id}`).innerText = [Object.keys(s.devices).length, Object.keys(s.cloud).length, s.sync.synced.length, total][i]));
    if($('stat-issues-detail')) $('stat-issues-detail').innerHTML = total ? [s.sync.missing.length && `<span class="text-rose-400">${s.sync.missing.length} Miss</span>`, s.sync.mismatched.length && `<span class="text-amber-400">${s.sync.mismatched.length} Mismatch</span>`, s.sync.orphaned.length && `<span class="text-slate-400">${s.sync.orphaned.length} Orphan</span>`].filter(Boolean).join(' / ') : '<span class="text-slate-500">All Good</span>';
};

const renderSync = () => {
    const row = (name, id, btn, clss, onC, onE) => `<div class="flex justify-between items-center text-sm border-b border-slate-700/50 py-2"><span class="truncate max-w-[200px]">${name} <span class="text-slate-500 text-xs">(${id})</span></span><div class="flex gap-2"><button onclick="${onE}" class="px-2 py-1 bg-slate-700 rounded hover:bg-slate-600"><i class="fa-solid fa-pen"></i></button><button onclick="${onC}" class="${clss} px-3 py-1 border rounded text-xs">${btn}</button></div></div>`;
    if($('body-missing')) $('body-missing').innerHTML = s.sync.missing.map(d => row(d.name, d.id, 'Import', 'text-emerald-400 border-emerald-500/20 bg-emerald-500/10', `resolve('missing','${d.id}')`, `openMod('${d.id}','cloud')`)).join('');
    if($('body-mismatch')) $('body-mismatch').innerHTML = s.sync.mismatched.map(m => row(m.cloud.name, m.cloud.id, 'Push', 'text-amber-400 border-amber-500/20 bg-amber-500/10', `resolve('mismatched','${m.cloud.id}')`, `openMod('${m.cloud.id}','cloud')`) + `<div class="text-[10px] text-amber-500 px-1 -mt-1 mb-2">Conflicts: ${m.reasons.join(', ')}</div>`).join('');
    
    // Orphans as a minimal table
    if($('body-orphan-rows')) $('body-orphan-rows').innerHTML = s.sync.orphaned.map(d => `<tr class="border-b border-slate-700/50 hover:bg-slate-800 cursor-pointer" onclick="openDet('${d.id}')"><td class="py-2 px-3">${d.name||'Unk'}</td><td class="py-2 px-3 text-xs font-mono text-slate-400">${d.id}</td></tr>`).join('');
    
    ['missing','mismatch','orphan','synced'].forEach(x => {
        if($(`count-${x}`)) $(`count-${x}`).innerText = s.sync[x === 'mismatch' ? 'mismatched' : (x === 'orphan' ? 'orphaned' : x)].length;
        if($(`section-${x}`)) $(`section-${x}`).classList.toggle('hidden', s.sync[x === 'mismatch' ? 'mismatched' : (x === 'orphan' ? 'orphaned' : x)].length === 0);
    });
};

const statCell = d => {
    if(d._missParent) return `<span class="status-dot bg-amber-500"></span><span class="text-amber-500 text-xs">No Parent</span>`;
    if(hasErr(d)) return `<span class="status-dot bg-red-500"></span><span class="text-red-500 text-xs">${s.errs[d.id] || 'Error'}</span>`;
    if(['subdevice','no parent'].includes(d.status)) return `<span class="status-dot bg-blue-500"></span><span class="text-blue-400 text-xs">Sub</span>`;
    const on = d.status === 'online' || d.status === true || d.status == 0;
    return `<span class="status-dot ${on ? 'status-online':'status-offline'}"></span><span class="text-slate-400 text-xs">${on?'Online':'Offline'}</span>`;
};

const buildTree = devs => {
    const root = [], map = {}, ids = new Set(devs.map(d => d.id));
    devs.forEach(d => {
        const p = d.parent || d.parent_id;
        if(p && ids.has(p)) (map[p]||(map[p]=[])).push(d);
        else { if(p) d._missParent = p; root.push(d); }
    });
    return { root, map };
};

const renderDashboard = () => {
    const tb = $('devices-body'), q = ($('search-input')?.value || '').toLowerCase();
    if (!tb) return;
    const devs = (s.sync.synced.length ? s.sync.synced : Object.values(s.devices)).filter(d => s.filter==='all' || (s.filter==='subdevice' && ['subdevice','no parent'].includes(d.status)) || (s.filter==='online' && !['subdevice','no parent'].includes(d.status) && (d.status==='online'||d.status===true||d.status==0)) || (s.filter==='offline' && !['subdevice','no parent'].includes(d.status) && d.status!=='online'&&d.status!==true&&d.status!=0));
    
    if(!devs.length) return tb.innerHTML = `<tr><td colspan="5" class="py-8 text-center text-slate-500">No devices</td></tr>`;
    
    const { root, map } = buildTree(devs);
    let html = '';
    const addRow = (d, indent) => {
        if(q && !`${d.name} ${d.id}`.toLowerCase().includes(q)) return;
        const ind = q ? 0 : indent * 2, zb = isZigbee(d), live = s.live[d.id];
        html += `<tr onclick="openDet('${d.id}')" class="border-b border-slate-700/50 hover:bg-slate-800 cursor-pointer">
            <td class="hidden md:table-cell py-3 px-4"><div style="padding-left:${ind}rem">${indent && !q ? '<i class="fa-solid fa-level-up-alt fa-rotate-90 mr-2 text-slate-600"></i>' : ''}${statCell(d)}</div></td>
            <td class="py-3 px-4 text-sm text-slate-400"><i class="fa-solid ${zb?'fa-network-wired':'fa-wifi'} mr-2 w-4"></i>${zb?'Zigbee':'WiFi'}
                <div class="md:hidden text-xs truncate max-w-[120px]">${d.name||'Unk'} ${live?'<span class="text-emerald-500">● live</span>':''}</div></td>
            <td class="hidden md:table-cell py-3 px-4 text-white">${d.name||'Unnamed'} ${live?'<span class="text-emerald-500 text-xs ml-2">● live</span>':''}</td>
            <td class="py-3 px-4 text-xs font-mono text-slate-400">${d.id}</td>
            <td class="hidden md:table-cell py-3 px-4 text-right"><i class="fa-solid fa-chevron-right text-slate-600"></i></td>
        </tr>`;
        if(!q) (map[d.id]||[]).forEach(c => addRow(c, indent+1));
    };
    (q ? devs : (root.length ? root : devs)).forEach(d => addRow(d, 0));
    tb.innerHTML = html;
    
    const pSel = $('dev-parent');
    if(pSel) {
        const v = pSel.value;
        pSel.innerHTML = '<option value="">Select...</option>' + Object.values(s.devices).filter(d=>!d.parent&&!d.sub&&!d.cid&&d.status!=='subdevice').map(d=>`<option value="${d.id}">${d.name||'Unk'} (${d.id})</option>`).join('');
        pSel.value = Array.from(pSel.options).some(o=>o.value===v) ? v : '';
    }
};

const renderDetails = () => {
    if(!s.curr || !$('details-content')) return;
    const d = s.devices[s.curr]; if(!d) return;
    
    let html = Object.entries(d).filter(([k,v])=>!['dps','_missParent','_is_cloud'].includes(k) && typeof v!=='object').map(([k,v]) => {
        if(['key','local_key','localkey'].includes(k)) {
            const uid = Math.random().toString(36).slice(2);
            return `<div class="flex justify-between items-center py-2 border-b border-slate-700/50"><span class="text-xs text-slate-500 uppercase">${k}</span><div class="flex items-center gap-2"><span id="v-${uid}" class="text-sm font-mono text-slate-400">••••••••</span><button onclick="$('v-${uid}').innerText=$('v-${uid}').innerText.includes('•')?'${v}':'••••••••'" class="text-slate-500 hover:text-white"><i class="fa-solid fa-eye"></i></button></div></div>`;
        }
        return `<div class="flex justify-between items-center py-2 border-b border-slate-700/50"><span class="text-xs text-slate-500 uppercase">${k}</span><span class="text-sm text-slate-300 truncate max-w-[200px]" title="${v}">${v}</span></div>`;
    }).join('');
    if(d._missParent) html += `<div class="mt-3 p-2 bg-amber-500/10 border border-amber-500/20 rounded text-amber-500 text-xs">Missing Parent: ${d._missParent}</div>`;
    $('details-content').innerHTML = html;

    const lSec = $('live-values-section'), lBody = $('live-values-body'), eSec = $('device-error-section'), eBody = $('device-error-body');
    if(hasErr(d)) {
        lSec.classList.add('hidden'); eSec.classList.remove('hidden');
        eBody.innerHTML = `<div class="text-red-400 text-xs"><i class="fa-solid fa-triangle-exclamation mr-2"></i>${s.errs[s.curr] || 'Error Code: ' + d.status}</div>`;
    } else {
        eSec.classList.add('hidden');
        const vals = s.live[s.curr];
        if(vals && Object.keys(vals).length) {
            lSec.classList.remove('hidden');
            lBody.innerHTML = Object.entries(vals).map(([dp, v])=>`<div class="flex justify-between items-center py-1 border-b border-slate-700/50 text-xs"><span class="font-mono text-slate-400">${dp}</span><span class="text-emerald-400">${JSON.stringify(v.val)}</span><span class="text-slate-600 text-[10px]">${v.ts}</span></div>`).join('');
        } else lSec.classList.add('hidden');
    }
};

const render = () => { renderStatus(); renderLogs(); renderStats(); renderSync(); renderDashboard(); renderDetails(); };

// =============================================================================
// Websocket & Event Handlers
// =============================================================================
const connectWS = () => {
    s.ws = new WebSocket(`ws://${window.location.host}/ws`);
    s.ws.onopen = () => { s.connected = true; sendCmd('status'); update(); };
    s.ws.onclose = () => { s.connected = s.mqtt = false; update(); setTimeout(connectWS, 5000); };
    s.ws.onerror = () => s.ws.close();
    s.ws.onmessage = ({ data }) => {
        const m = JSON.parse(data);
        if(m.type === 'mqtt_status') { s.mqtt = m.connected; return update(); }
        if(m.type === 'wizard') return handleWiz(m.status, m.cloud_devices);
        if(m.type === 'bridge_response') return (showToast(m.message, m.level), addLog(m.message, m.level));
        
        if(m.type === 'mqtt') {
            const p = m.payload || {}, eMsg = p.errorMsg || p.message || p.error || (p.errorCode ? `Err ${p.errorCode}` : ''), t = m.topic_type;
            if(t === 'response') { showToast(`Bridge OK: ${eMsg || 'Success'}`, 'success'); addLog(`Bridge OK: ${eMsg || 'Success'}`, 'success'); }
            if(t === 'error') {
                const realErr = p.errorCode !== 0 && p.status !== 'success';
                if(realErr && p.id && s.errs[p.id] !== eMsg) { showToast(`Err: ${eMsg}`, 'error'); addLog(`Err: ${eMsg}`, 'error'); s.errs[p.id] = eMsg; }
                else if(!realErr && p.id) delete s.errs[p.id];
            }
            if(t === 'event' && p && !p.action) {
                const d = p.dps ?? p.data ?? p;
                if(typeof d==='object' && !Array.isArray(d)) {
                    s.live[m.id||p.id] = s.live[m.id||p.id] || {};
                    Object.entries(d).forEach(([k,v]) => !['id','name','cid','data'].includes(k) && (s.live[m.id||p.id][k] = {val:v, ts:new Date().toLocaleTimeString()}));
                }
            }
        }
        if(m.type === 'init' || m.devices_updated || m.type === 'status') {
            if(m.cloud_devices) s.cloud = m.cloud_devices;
            if(m.devices) s.devices = m.devices;
            if(m.mqtt_connected !== undefined) s.mqtt = m.mqtt_connected;
            if(m.type === 'init' && m.user_code && $('wizard-code')) $('wizard-code').value = m.user_code;
            update();
        }
    };
};

const handleWiz = (st, cd) => {
    if(cd) s.cloud = cd;
    if(st.error) {
        showToast(st.error, 'error'); addLog(`Wizard Err: ${st.error}`, 'error');
        $('wizard-spinner').classList.add('hidden'); $('wizard-status-title').innerText = 'Error'; $('wizard-status-msg').innerText = st.error;
    } else {
        $('wizard-status-title').innerText = st.step;
        $('wizard-qr-container').classList.toggle('hidden', !st.url);
        $('wizard-spinner').classList.toggle('hidden', !!st.url);
        if(st.url && window.qrcode) { const qr = qrcode(0,'L'); qr.addData(st.url); qr.make(); $('wizard-qr-img').src = qr.createDataURL(6); }
        if(!st.running) { showToast('Wizard Complete', 'success'); addLog('Wizard Complete', 'success'); setTimeout(() => { closeAllModals(); sendCmd('status'); }, 1500); }
    }
    update();
};

// =============================================================================
// Interactions
// =============================================================================
const addDev = d => {
    const p = { id: d.id, name: d.name || 'Unk' };
    if(isZigbee(d)) { p.cid = d.cid || d.node_id; p.parent_id = d.parent_id || d.parent; }
    else { p.key = d.key || d.local_key || d.localkey; p.ip = isPrivIP(d.ip) ? d.ip : 'Auto'; p.version = d.version || 'Auto'; }
    sendCmd('add', p);
};

const resolveAll = async (cat) => {
    const arr = s.sync[cat], c = arr.length; if(!c) return;
    if(!await showConf(`Process all ${c} devices?`)) return;
    arr.forEach(x => cat === 'orphaned' ? sendCmd('remove', {id: x.id}) : addDev(cat === 'mismatched' ? x.cloud : x));
};
const resolve = (cat, id) => {
    const d = s.sync[cat].find(x => (x.id || x.cloud?.id) === id);
    if(d) cat === 'orphaned' ? sendCmd('remove', {id: d.id}) : addDev(cat === 'mismatched' ? d.cloud : d);
};

window.resolveMissing = () => resolveAll('missing');
window.resolveMismatch = window.resolveMismatched = () => resolveAll('mismatched');
window.resolveOrphans = () => resolveAll('orphaned');

const toggleSide = () => { $('sidebar').classList.toggle('-translate-x-full'); $('sidebar-backdrop').classList.toggle('hidden'); };
const closeSide = () => { $('sidebar').classList.add('-translate-x-full'); $('sidebar-backdrop').classList.add('hidden'); };
const showSect = (id, e) => { $$('.section').forEach(x => x.classList.add('hidden')); $(id).classList.remove('hidden'); $$('.nav-item').forEach(x => x.classList.remove('active')); if(e) e.currentTarget.classList.add('active'); if(window.innerWidth<1024) closeSide(); };
const toggleLog = () => { $('log-panel').classList.toggle('translate-x-full'); $('details-panel').classList.add('translate-x-full'); $('panel-backdrop').classList.toggle('hidden', $('log-panel').classList.contains('translate-x-full')); };
const closeLogs = () => { $('log-panel').classList.add('translate-x-full'); $('panel-backdrop').classList.add('hidden'); };

const openDet = id => { s.curr = id; $('details-panel').classList.remove('translate-x-full'); $('panel-backdrop').classList.remove('hidden'); $('log-panel').classList.add('translate-x-full'); update(); };
const closeDet = () => { s.curr = null; $('details-panel').classList.add('translate-x-full'); $('panel-backdrop').classList.add('hidden'); update(); };
const closeAllModals = () => { $$('.modal-container').forEach(m => m.classList.add('hidden','scale-95')); $('modal-overlay').classList.add('opacity-0','pointer-events-none'); };
const showMod = id => { $('modal-overlay').classList.remove('opacity-0','pointer-events-none'); $(id).classList.remove('scale-95','hidden'); };

const openMod = (id=null, src='bridge') => {
    const d = id ? (src==='cloud' ? s.cloud[id] : s.devices[id]) : null;
    $('modal-title').innerText = d ? (src==='cloud'?'Import':'Edit') : 'Add';
    $('dev-id').value = d?.id || ''; $('dev-id').readOnly = !!d; $('dev-name').value = d?.name || '';
    const zb = d ? isZigbee(d) : false;
    document.querySelector(`input[name="dev-type"][value="${zb?'Zigbee/BLE':'WiFi'}"]`).checked = true;
    $('fields-wifi').classList.toggle('hidden', zb); $('fields-zigbee').classList.toggle('hidden', !zb);
    if(zb) { $('dev-node').value = d?.cid||d?.node_id||''; $('dev-parent').value = d?.parent_id||d?.parent||''; }
    else { $('dev-key').value = d?.key||d?.local_key||d?.localkey||''; $('dev-ip').value = isPrivIP(d?.ip)?d.ip:''; $('dev-version').value = d?.version||''; }
    showMod('device-modal');
};

const showConf = (msg, title='Confirm') => new Promise(r => {
    closeAllModals(); $('confirm-title').innerText = title; $('confirm-message').innerText = msg; showMod('confirm-modal');
    const cln = res => { closeAllModals(); $('confirm-ok-btn').onclick = null; $('confirm-cancel-btn').onclick = null; r(res); };
    $('confirm-ok-btn').onclick = () => cln(true); $('confirm-cancel-btn').onclick = () => cln(false);
});

$('device-form')?.addEventListener('submit', e => {
    e.preventDefault();
    const isW = document.querySelector('input[name="dev-type"]:checked').value === 'WiFi';
    sendCmd('add', { id: $('dev-id').value, name: $('dev-name').value, ...(isW ? { key: $('dev-key').value, ip: $('dev-ip').value||'Auto', version: $('dev-version').value||'Auto' } : { cid: $('dev-node').value, parent_id: $('dev-parent').value }) });
    closeAllModals(); closeDet();
});

$$('input[name="dev-type"]').forEach(r => r.addEventListener('change', () => { $('fields-wifi').classList.toggle('hidden', r.value!=='WiFi'); $('fields-zigbee').classList.toggle('hidden', r.value==='WiFi'); }));

window.startWizard = () => {
    const c = $('wizard-code').value; if(!c) return showToast('Enter code','warning');
    $('wizard-input-step').classList.add('hidden'); $('wizard-loading-step').classList.remove('hidden'); $('wizard-spinner').classList.remove('hidden');
    sendCmd('wizard_start', { user_code: c });
};

window.setFilter = f => { s.filter = f; $$('.filter-btn').forEach(b => b.classList.toggle('active-filter', b.dataset.filter === f)); update(); };
window.requestSyncCheck = window.requestStatusUpdate = () => sendCmd('status');
window.openAddDeviceModal = () => openMod();
window.openEditDeviceModal = id => openMod(id, 'bridge');
window.openDeviceImportModal = id => openMod(id, 'cloud');
window.openWizardModal = () => { closeAllModals(); $('wizard-input-step').classList.remove('hidden'); $('wizard-loading-step').classList.add('hidden'); $('wizard-qr-container').classList.add('hidden'); showMod('wizard-modal'); };
window.closeModal = closeAllModals;
window.closeDetails = closeDet;
window.closeLogPanel = closeLogs;
window.toggleLogPanel = toggleLog;
window.toggleSidebar = toggleSide;
window.closeSidebar = closeSide;
window.showSection = showSection;
document.addEventListener('keydown', e => e.key === 'Escape' && (closeAllModals(), closeDet(), closeLogs()));

$('btn-edit')?.addEventListener('click', () => { closeDet(); openMod(s.curr, 'bridge'); });
$('btn-delete')?.addEventListener('click', async () => { if(await showConf(`Delete ${s.devices[s.curr]?.name||s.curr}?`)) { sendCmd('remove', {id: s.curr}); delete s.devices[s.curr]; closeDet(); update(); } });

// Init
connectWS();
