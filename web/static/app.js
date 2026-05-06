// =============================================================================
// State & Constants
// =============================================================================
const MAX_LOG = 100;
const META_KEYS = new Set(['id', 'name', 'cid', 'data']);
const HIDDEN_DETAIL_KEYS = new Set(['dps', '_missing_parent', '_is_cloud']);
const MASKED_DETAIL_KEYS = new Set(['key', 'local_key', 'localkey']);

let ws = null, reconnectTimer = null, currentDeviceId = null;
let devices_map = {}, cloud_devices = {}, liveValues = {}, deviceErrors = {}, activityLog = [];
let currentFilter = 'all';
let currentSyncData = { missing: [], mismatched: [], orphaned: [], synced: [] };
let _maskedKeyRevealState = {};

// =============================================================================
// DOM Utilities
// =============================================================================
const $ = (id) => document.getElementById(id);
const toggle = (id, show) => $(id)?.classList.toggle('hidden', !show);
const setVal = (id, val) => { const el = $(id); if (el) el.value = val; };
const setHTML = (id, html) => { const el = $(id); if (el) el.innerHTML = html; };
const setText = (id, text) => { const el = $(id); if (el) el.innerText = text; };
const toggleClass = (id, cls, state) => $(id)?.classList.toggle(cls, state);

// =============================================================================
// Toast Notification System
// =============================================================================
function showToast(message, level = 'info', duration = 3500) {
    const container = $('toast-container');
    if (!container) return;

    if (container.children.length >= 5) container.children[0].remove();

    const themes = {
        success: { bg: 'bg-emerald-600 border-emerald-500', icon: 'fa-circle-check' },
        error:   { bg: 'bg-red-700 border-red-600', icon: 'fa-circle-xmark' },
        info:    { bg: 'bg-slate-700 border-slate-600', icon: 'fa-circle-info' },
        warning: { bg: 'bg-amber-600 border-amber-500', icon: 'fa-triangle-exclamation' },
    };
    const t = themes[level] || themes.info;

    const toast = document.createElement('div');
    toast.className = `flex items-start gap-3 px-4 py-3 rounded-lg border shadow-2xl text-white text-sm transition-all duration-300 opacity-0 -translate-y-4 max-w-md ${t.bg}`;
    toast.innerHTML = `<i class="fa-solid ${t.icon} mt-0.5 shrink-0"></i><span class="break-words overflow-hidden">${message}</span>`;
    container.appendChild(toast);

    requestAnimationFrame(() => toast.classList.remove('opacity-0', '-translate-y-4'));
    setTimeout(() => {
        toast.classList.add('opacity-0', '-translate-y-4');
        setTimeout(() => toast.remove(), 300);
    }, duration);
}

// =============================================================================
// Activity Log
// =============================================================================
function addLog(message, level = 'info') {
    activityLog.unshift({ ts: new Date().toLocaleTimeString(), message, level });
    if (activityLog.length > MAX_LOG) activityLog.pop();
    renderActivityLog();
}

function renderActivityLog() {
    const colors = { info: 'text-slate-300', error: 'text-red-400', success: 'text-emerald-400', warning: 'text-amber-400' };
    setHTML('log-body', activityLog.map(({ ts, message, level }) =>
        `<div class="flex gap-3 text-xs py-1.5 border-b border-slate-700/50 last:border-0">
            <span class="text-slate-500 shrink-0 font-mono">${ts}</span>
            <span class="${colors[level] || colors.info}">${message}</span>
         </div>`
    ).join('') || `<p class="text-slate-500 text-sm text-center py-6">No activity yet.</p>`);
}

function toggleLogPanel() {
    const isVisible = $('log-panel').classList.toggle('translate-x-full');
    if (!isVisible) showPanelBackdrop();
    else hidePanelBackdrop();
    toggleClass('details-panel', 'translate-x-full', true);
}

function closeLogPanel() {
    toggleClass('log-panel', 'translate-x-full', true);
    hidePanelBackdrop();
}

// =============================================================================
// WebSocket
// =============================================================================
// =============================================================================
// WebSocket Dispatcher
// =============================================================================
const WS_HANDLERS = {
    mqtt_status: (msg) => {
        updateMqttBrokerStatus(msg.connected);
        showToast(`MQTT broker ${msg.connected ? 'connected' : 'disconnected'}`, msg.connected ? 'success' : 'warning', 2000);
    },
    wizard: (msg) => {
        handleWizardEvent(msg.status);
        if (msg.cloud_devices) { cloud_devices = msg.cloud_devices; updateSyncStateAndRender(); }
    },
    bridge_response: (msg) => {
        const level = msg.level === 'error' ? 'error' : 'success';
        showToast(msg.message, level);
        addLog(msg.message, level);
    },
    mqtt: (msg) => {
        if (msg.topic_type === 'response' || msg.topic_type === 'error') {
            const p = msg.payload || {};
            const isError = msg.topic_type === 'error';
            let text = p.message || p.errorMsg || p.error;
            
            if (!text) {
                if (p.action === 'status') text = `Status updated (${Object.keys(p.devices || {}).length} devices)`;
                else if (p.action === 'remove') text = `Device removed: ${p.id}`;
                else text = JSON.stringify(p);
            }

            const fullText = `${p.name ? `[${p.name}] ` : ''}${p.id ? `(${p.id}) ` : ''}${text}`;
            const isRealError = isError && p.errorCode !== 0 && p.status !== 'success';
            
            if (!isRealError || deviceErrors[p.id] !== fullText) {
                showToast(`Bridge: ${fullText}`, isRealError ? 'error' : 'success');
                addLog(`Bridge ${isRealError ? 'ERR' : 'OK'} — ${fullText}`, isRealError ? 'error' : 'success');
            }

            if (p.id) {
                if (isRealError) deviceErrors[p.id] = fullText;
                else if (p.errorCode === 0) delete deviceErrors[p.id];
                if (currentDeviceId === p.id) updateDetailsLiveValues(p.id);
            }
        } else if (msg.topic_type === 'event' && !msg.payload?.action) {
            const did = msg.payload?.id || msg.id;
            const raw = msg.payload?.dps ?? msg.payload?.data ?? msg.payload;
            if (did && typeof raw === 'object' && !Array.isArray(raw)) {
                const ts = new Date().toLocaleTimeString();
                liveValues[did] = { ...(liveValues[did] ?? {}) };
                for (const [dp, v] of Object.entries(raw)) {
                    if (!META_KEYS.has(dp)) liveValues[did][dp] = { value: v, ts };
                }
                if (currentDeviceId === did) updateDetailsLiveValues(did);
                renderDashboard();
            }
        }
    },
    init_status: (msg) => {
        if (msg.cloud_devices) cloud_devices = msg.cloud_devices;
        if (msg.devices) devices_map = msg.devices;
        if (msg.mqtt_connected !== undefined) updateMqttBrokerStatus(msg.mqtt_connected);
        if (msg.type === 'init' && msg.user_code) setVal('wizard-code', msg.user_code);
        updateSyncStateAndRender();
    }
};

function connectWS() {
    ws = new WebSocket(`ws://${window.location.host}/ws`);
    ws.onopen = () => { clearTimeout(reconnectTimer); updateConnectionStatus(true); requestStatusUpdate(); };
    ws.onmessage = ({ data }) => {
        const msg = JSON.parse(data);
        const handler = WS_HANDLERS[msg.type] || (msg.devices_updated ? WS_HANDLERS.init_status : null);
        if (handler) handler(msg);
        else if (msg.type === 'init' || msg.type === 'status') WS_HANDLERS.init_status(msg);
    };
    ws.onclose = () => { updateConnectionStatus(false); updateMqttBrokerStatus(false); reconnectTimer = setTimeout(connectWS, 5000); };
    ws.onerror = () => ws.close();
}

function sendCommand(action, payload = {}) {
    if (ws?.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ action, payload }));
        addLog(`→ ${action}${payload.id ? ` [${payload.id}]` : ''}`, 'info');
    } else showToast('Disconnected from backend', 'error');
}

function updateConnectionStatus(connected) {
    setHTML('connection-status', connected 
        ? `<span class="status-dot status-online"></span><span class="text-slate-300 font-medium tracking-wide">Connected</span>`
        : `<span class="relative flex h-3 w-3 mr-3"><span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-red-400 opacity-75"></span><span class="relative inline-flex rounded-full h-3 w-3 bg-red-500"></span></span><span class="text-slate-300 font-medium tracking-wide">Disconnected</span>`
    );
}

function updateMqttBrokerStatus(connected) {
    setHTML('mqtt-broker-status', `<span class="status-dot ${connected ? 'status-online' : 'status-offline'}"></span><span class="text-slate-400 text-xs tracking-wide">MQTT Broker</span>`);
}

// =============================================================================
// Sync state computation
// =============================================================================
function computeSyncState() {
    const result = { missing: [], mismatched: [], orphaned: [], synced: [] };
    const bridge_ids = Object.keys(devices_map);
    const cloud_ids = Object.keys(cloud_devices);

    if (!cloud_ids.length) { result.synced = Object.values(devices_map); return result; }

    for (const cid of cloud_ids) {
        const cdev = cloud_devices[cid], bdev = devices_map[cid];
        if (!bdev) { result.missing.push(cdev); continue; }
        
        const diff = [];
        if (cdev.name && bdev.name && cdev.name !== bdev.name) diff.push('name');
        const ckey = cdev.key || cdev.local_key || cdev.localkey, bkey = bdev.key || bdev.local_key;
        if (ckey && bkey && ckey !== bkey) diff.push('local_key');
        
        diff.length > 0 ? result.mismatched.push({ cloud: cdev, bridge: bdev, reasons: diff }) : result.synced.push(bdev);
    }

    for (const bid of bridge_ids) { if (!cloud_ids.includes(bid)) result.orphaned.push(devices_map[bid]); }
    return result;
}

function updateSyncStateAndRender() {
    currentSyncData = computeSyncState();
    updateStatsCards();
    renderSyncPanels();
    renderDashboard();
    if (currentDeviceId) updateDetailsLiveValues(currentDeviceId);
}

function updateStatsCards() {
    const { missing, mismatched, orphaned, synced } = currentSyncData;
    setText('stat-total', Object.keys(devices_map).length);
    setText('stat-cloud', Object.keys(cloud_devices).length);
    setText('stat-synced', synced.length);
    setText('stat-conflicts', missing.length + mismatched.length + orphaned.length);

    const parts = [];
    if (missing.length) parts.push(`<span class="text-rose-400 font-medium">${missing.length} Missing</span>`);
    if (mismatched.length) parts.push(`<span class="text-amber-400 font-medium">${mismatched.length} Mismatch</span>`);
    if (orphaned.length) parts.push(`<span class="text-slate-400 font-medium">${orphaned.length} Orphan</span>`);

    setHTML('stat-issues-detail', parts.length ? parts.join('<span class="text-slate-600 opacity-50">/</span>') : '<span class="text-slate-500 italic">No issues found</span>');
}

function requestStatusUpdate() {
    sendCommand('status');
    const icon = document.querySelector('.fa-rotate-right');
    if (icon) { icon.classList.add('fa-spin'); setTimeout(() => icon.classList.remove('fa-spin'), 1000); }
}

// =============================================================================
// Sync panels
// =============================================================================
function syncItemRow(label, id, btnText, btnClass, onClickExpr, onEditExpr) {
    return `<div class="flex flex-col md:flex-row md:justify-between md:items-center gap-2 text-sm border-b border-slate-700/50 pb-2 mb-2 last:border-0">
        <span class="text-white min-w-0 truncate">${label}</span>
        <div class="flex gap-2 shrink-0">
            ${onEditExpr ? `<button onclick="${onEditExpr}" class="px-2 py-1.5 rounded bg-slate-700 hover:bg-slate-600 text-slate-400 hover:text-white border border-slate-600"><i class="fa-solid fa-pen-to-square"></i></button>` : ''}
            <button onclick="${onClickExpr}" class="${btnClass} px-3 py-1.5 rounded transition-colors border text-sm font-medium">${btnText}</button>
        </div>
    </div>`;
}

function renderSyncPanels() {
    const { missing, mismatched, orphaned, synced } = currentSyncData;
    
    const render = (id, items, rowFn) => {
        setText(`count-${id}`, items.length);
        toggle(`section-${id}`, items.length > 0);
        setHTML(`body-${id}`, items.map(rowFn).join(''));
    };

    render('missing', missing, (dev) => syncItemRow(`${dev.name} <span class="text-slate-500 font-mono text-xs">(${dev.id})</span>`, dev.id, 'Import to Bridge', 'text-emerald-400 bg-emerald-500/10 border-emerald-500/20', `resolveSingle('missing', '${dev.id}', event)`, `openDeviceImportModal(cloud_devices['${dev.id}'])`));
    render('mismatch', mismatched, (item) => {
        const did = item.cloud.id;
        return `<div class="mb-2 last:mb-0">${syncItemRow(`${item.cloud.name} <span class="text-slate-500 font-mono text-xs">(${did})</span>`, did, 'Push to Bridge', 'text-amber-400 bg-amber-500/10 border-amber-500/20', `resolveSingle('mismatched', '${did}', event)`, `openDeviceImportModal(cloud_devices['${did}'])`)}<div class="text-[10px] text-amber-500/70 -mt-1.5 mb-2 px-1">Conflicts: ${item.reasons.join(', ')}</div></div>`;
    });

    setText('count-orphan', orphaned.length);
    toggle('section-orphan', orphaned.length > 0);
    if (orphaned.length) {
        setHTML('body-orphan', `<table class="w-full text-sm"><thead><tr class="text-left text-xs text-slate-500 border-b border-slate-700"><th class="hidden md:table-cell px-4 py-2">Status</th><th class="px-4 py-2">Type</th><th class="hidden md:table-cell px-4 py-2">Name</th><th class="px-4 py-2">ID</th><th class="hidden md:table-cell px-4 py-2"></th></tr></thead><tbody id="body-orphan-rows"></tbody></table>`);
        const tbody = $('body-orphan-rows');
        orphaned.forEach(dev => {
            const isZigbee = !!(dev.sub || dev.parent || dev.parent_id), hasLive = !!liveValues[dev.id];
            const tr = document.createElement('tr');
            tr.className = 'border-b border-slate-700/50 hover:bg-slate-800/40 cursor-pointer group';
            tr.onclick = () => openDetails(dev.id);
            tr.innerHTML = `<td class="hidden md:table-cell py-3 px-4">${renderStatusCell(dev)}</td><td class="py-3 px-4"><div class="flex items-center text-slate-400 gap-2"><i class="fa-solid ${isZigbee ? 'fa-network-wired' : 'fa-wifi'} w-4 text-center shrink-0"></i><div><span class="text-sm font-medium">${isZigbee ? 'Zigbee/BLE' : 'WiFi'}</span><div class="md:hidden text-xs text-slate-400 mt-0.5 truncate max-w-[120px]">${dev.name || 'Unnamed'}${hasLive ? '<span class="text-emerald-500">● live</span>' : ''}</div></div></div></td><td class="hidden md:table-cell py-3 px-4 font-medium text-white">${dev.name || 'Unnamed Device'}${hasLive ? '<span class="ml-2 text-xs text-emerald-500 font-normal">● live</span>' : ''}</td><td class="py-3 px-4 font-mono text-xs text-slate-400 truncate">${dev.id}</td><td class="hidden md:table-cell py-3 px-4 text-right"><button class="text-slate-500 hover:text-white p-2 rounded hover:bg-slate-700"><i class="fa-solid fa-chevron-right"></i></button></td>`;
            tbody.appendChild(tr);
        });
    }
    setText('count-synced', synced.length);
}

async function resolveAll(category, e) {
    if (e) e.stopPropagation();
    const count = currentSyncData[category].length;
    if (!count) return;

    const titles = { missing: 'Import All?', mismatched: 'Update All?', orphaned: 'Delete All?' };
    const msgs = { missing: `Import all ${count} missing devices?`, mismatched: `Update all ${count} mismatched devices?`, orphaned: `Delete all ${count} orphaned devices?` };
    
    if (await showConfirm({ title: titles[category], message: msgs[category] })) {
        const actions = { missing: (d) => submitDeviceBridgeAdd(d), mismatched: (i) => submitDeviceBridgeAdd(i.cloud), orphaned: (d) => sendCommand('remove', { id: d.id }) };
        currentSyncData[category].forEach(actions[category]);
    }
}

async function resolveSingle(cat, id, e) {
    if (e) e.stopPropagation();
    const item = currentSyncData[cat].find(x => (x.id ?? x.cloud?.id) === id);
    if (!item) return;
    if (cat === 'orphaned') {
        if (await showConfirm({ title: 'Delete Device?', message: `Delete "${item.name || item.id}"?` })) sendCommand('remove', { id: item.id });
    } else submitDeviceBridgeAdd(cat === 'mismatched' ? item.cloud : item);
}

const resolveMissing = (e) => resolveAll('missing', e), resolveOrphans = (e) => resolveAll('orphaned', e), resolveMismatch = (e) => resolveAll('mismatched', e), resolveMismatched = (e) => resolveAll('mismatched', e);

function isPrivateIP(ip) {
    if (!ip || typeof ip !== 'string') return false;
    if (ip.toLowerCase() === 'auto') return true;
    const [a, b] = ip.split('.');
    return a === '10' || (a === '192' && b === '168') || (a === '172' && +b >= 16 && +b <= 31);
}

function submitDeviceBridgeAdd(dev) {
    const payload = { id: dev.id, name: dev.name || 'Unnamed' };
    if (dev.sub || dev.parent || dev.parent_id || dev.node_id || dev.cid) {
        payload.cid = dev.cid || dev.node_id;
        payload.parent_id = dev.parent_id || dev.parent;
    } else {
        Object.assign(payload, { key: dev.key || dev.local_key || dev.localkey, ip: isPrivateIP(dev.ip) ? dev.ip : 'Auto', version: dev.version || 'Auto' });
    }
    sendCommand('add', payload);
}

// =============================================================================
// Tree helper (shared by renderDashboard + renderTree)
// =============================================================================
function buildTree(devices) {
    const rootNodes = [];
    const childMap = {};
    const deviceIds = new Set(devices.map(d => d.id));

    for (const d of devices) {
        const parentId = d.parent || d.parent_id;
        if (parentId && deviceIds.has(parentId)) {
            (childMap[parentId] ??= []).push(d);
        } else {
            // No parent or parent not in current list -> treat as root
            if (parentId) d._missing_parent = parentId;
            rootNodes.push(d);
        }
    }
    return { rootNodes, childMap };
}

// =============================================================================
// Status filter
// =============================================================================
function setFilter(f) {
    currentFilter = f;
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.toggle('active-filter', b.dataset.filter === f));
    renderDashboard();
}

function passesFilter(dev) {
    if (currentFilter === 'all') return true;
    const isSub = ['subdevice', 'no parent', 'invalid subdevice'].includes(dev.status);
    if (currentFilter === 'subdevice') return isSub;
    if (currentFilter === 'online') return !isSub && (dev.status === 'online' || dev.status === true || dev.status === '0' || dev.status === 0);
    if (currentFilter === 'offline') return !isSub && (dev.status === 'offline' || (typeof dev.status === 'string' && /^\d+$/.test(dev.status) && dev.status !== '0'));
    return true;
}

function renderStatusCell(dev) {
    const err = deviceErrors[dev.id], online = dev.status === 'online' || dev.status === true || dev.status === '0' || dev.status === 0;
    if (dev._missing_parent) return `<span class="status-dot bg-amber-500 shadow-[0_0_8px_rgba(245,158,11,0.5)]"></span><span class="text-sm font-medium text-amber-500">Missing Parent${cloud_devices[dev._missing_parent] ? ' (Cloud)' : ''}</span>`;
    const isSub = ['subdevice', 'no parent', 'invalid subdevice'].includes(dev.status);
    const isErrCode = !online && !isSub && typeof dev.status === 'string' && /^\d+$/.test(dev.status);
    if (err || isErrCode) return `<span class="status-dot bg-red-500 shadow-[0_0_8px_rgba(239,68,68,0.5)]"></span><span class="text-sm font-medium text-red-500">${isErrCode ? `Error ${dev.status}` : 'Error'}</span>`;
    if (isSub) return `<span class="status-dot bg-blue-500 shadow-[0_0_0_2px_rgba(59,130,246,0.25)]"></span><span class="text-sm font-medium text-blue-400">Sub-device</span>`;
    return `<span class="status-dot ${online ? 'status-online' : 'status-offline'}"></span><span class="text-sm font-medium ${online ? 'text-slate-300' : 'text-slate-500'}">${online ? 'Online' : 'Offline'}</span>`;
}

function renderDashboard() {
    const tbody = $('devices-body'), search = $('search-input')?.value.toLowerCase() || '';
    const devices = (currentSyncData.synced.length ? currentSyncData.synced : Object.values(devices_map)).filter(passesFilter);
    
    if (!devices.length) { tbody.innerHTML = `<tr><td colspan="5" class="py-12 text-center text-slate-500">No devices match.</td></tr>`; return; }

    const { rootNodes, childMap } = buildTree(devices);
    tbody.innerHTML = '';

    const appendRow = (dev, indent) => {
        if (search && !dev.name?.toLowerCase().includes(search) && !dev.id?.toLowerCase().includes(search)) return;
        const isZigbee = !!(dev.sub || dev.parent || dev.parent_id), hasLive = !!liveValues[dev.id], err = deviceErrors[dev.id];
        const statusBadge = err ? `<span class="text-red-500 animate-pulse">● err</span>` : (hasLive ? `<span class="text-emerald-500">● live</span>` : '');
        
        const tr = document.createElement('tr');
        tr.className = 'border-b border-slate-700/50 hover:bg-slate-800/40 cursor-pointer group';
        tr.onclick = () => openDetails(dev.id);
        tr.innerHTML = `
            <td class="hidden md:table-cell py-4 px-5"><div class="flex items-center" style="padding-left:${search ? 0 : indent * 2}rem">${(indent > 0 && !search) ? `<i class="fa-solid fa-level-up-alt fa-rotate-90 text-slate-600 mr-2 opacity-70"></i>` : ''}${renderStatusCell(dev)}</div></td>
            <td class="py-2.5 md:py-4 px-3 md:px-5"><div class="flex items-center text-slate-400 group-hover:text-blue-400 transition-colors"><i class="fa-solid ${isZigbee ? 'fa-network-wired' : 'fa-wifi'} mr-2 w-4 text-center"></i><div class="min-w-0"><span class="text-sm font-medium">${isZigbee ? 'Zigbee/BLE' : 'WiFi'}</span><div class="md:hidden text-xs text-slate-400 mt-0.5 truncate max-w-[140px]">${dev.name || 'Unnamed'}${statusBadge ? ' ' + statusBadge : ''}</div></div></div></td>
            <td class="hidden md:table-cell py-4 px-5 text-sm font-medium text-white">${dev.name || 'Unnamed Device'}${err ? `<span class="ml-2 text-xs text-red-500 animate-pulse">● error</span>` : (hasLive ? `<span class="ml-2 text-xs text-emerald-500">● live</span>` : '')}</td>
            <td class="py-2.5 md:py-4 px-3 md:px-5 font-mono text-xs text-slate-400 group-hover:text-slate-300 truncate">${dev.id}</td>
            <td class="hidden md:table-cell py-4 px-5 text-right"><button class="text-slate-500 hover:text-white p-2 rounded hover:bg-slate-700" onclick="event.stopPropagation(); openDetails('${dev.id}')"><i class="fa-solid fa-chevron-right"></i></button></td>`;
        tbody.appendChild(tr);
        if (!search) (childMap[dev.id] || []).forEach(c => appendRow(c, indent + 1));
    };

    (search ? devices : (rootNodes.length ? rootNodes : devices)).forEach(d => appendRow(d, 0));
    populateParentsSelect();
}

// renderTree() — topology view, currently unused but preserved
// function renderTree() { ... }

// =============================================================================
// Modals
// =============================================================================
function showModal(id) { 
    toggleClass('modal-overlay', 'opacity-0', false); 
    toggleClass('modal-overlay', 'pointer-events-none', false); 
    toggle(id, true); 
    setTimeout(() => toggleClass(id, 'scale-95', false), 10);
}

function closeModal() {
    toggleClass('modal-overlay', 'opacity-0', true); 
    toggleClass('modal-overlay', 'pointer-events-none', true);
    ['device-modal', 'wizard-modal', 'sync-modal', 'confirm-modal'].forEach(id => {
        toggleClass(id, 'scale-95', true);
        setTimeout(() => toggle(id, false), 300);
    });
}

function openAddDeviceModal() {
    currentDeviceId = null;
    setText('modal-title', 'Add Device');
    $('device-form').reset();
    $('dev-id').readOnly = false;
    toggleDeviceFields();
    showModal('device-modal');
}

function openEditDeviceModal(id) {
    const dev = devices_map[id];
    if (!dev) return;
    currentDeviceId = id;
    setText('modal-title', 'Edit Device');
    setVal('dev-id', id);
    $('dev-id').readOnly = true;
    setVal('dev-name', dev.name || '');
    const isZig = !!(dev.parent || dev.sub || dev.node_id);
    document.querySelector(`input[name="dev-type"][value="${isZig ? 'Zigbee/BLE' : 'WiFi'}"]`).checked = true;
    toggleDeviceFields();
    if (isZig) { setVal('dev-node', dev.node_id || dev.cid || ''); setVal('dev-parent', dev.parent || ''); }
    else { setVal('dev-key', dev.key || dev.local_key || ''); setVal('dev-ip', dev.ip || ''); setVal('dev-version', dev.version || ''); }
    showModal('device-modal');
}

function openDeviceImportModal(dev) {
    currentDeviceId = null;
    setText('modal-title', 'Import Device');
    setVal('dev-id', dev.id);
    $('dev-id').readOnly = true;
    setVal('dev-name', dev.name || '');
    const isZig = !!(dev.sub || dev.parent || dev.parent_id || dev.node_id || dev.cid);
    document.querySelector(`input[name="dev-type"][value="${isZig ? 'Zigbee/BLE' : 'WiFi'}"]`).checked = true;
    toggleDeviceFields();
    if (isZig) { setVal('dev-node', dev.cid || dev.node_id || ''); setVal('dev-parent', dev.parent_id || dev.parent || ''); }
    else { setVal('dev-key', dev.key || dev.local_key || dev.localkey || ''); setVal('dev-ip', isPrivateIP(dev.ip) ? dev.ip : ''); setVal('dev-version', dev.version || ''); }
    showModal('device-modal');
}

function openWizardModal() {
    ['device-modal', 'sync-modal'].forEach(id => toggle(id, false));
    ['wizard-qr-container', 'wizard-spinner', 'wizard-loading-step'].forEach(id => toggle(id, false));
    toggle('wizard-input-step', true);
    showModal('wizard-modal');
}

function showConfirm({ title = 'Are you sure?', message = '', okText = 'Confirm', cancelText = 'Cancel' }) {
    return new Promise((res) => {
        ['device-modal', 'wizard-modal', 'sync-modal'].forEach(id => toggle(id, false));
        setText('confirm-title', title);
        setText('confirm-message', message);
        setText('confirm-ok-btn', okText);
        setText('confirm-cancel-btn', cancelText);
        toggleClass('modal-overlay', 'opacity-0', false);
        toggleClass('modal-overlay', 'pointer-events-none', false);
        toggle('confirm-modal', true);
        setTimeout(() => toggleClass('confirm-modal', 'scale-95', false), 10);
        
        const cleanup = (v) => {
            toggleClass('confirm-modal', 'scale-95', true);
            window.removeEventListener('keydown', onKey);
            setTimeout(() => { toggle('confirm-modal', false); toggleClass('modal-overlay', 'opacity-0', true); toggleClass('modal-overlay', 'pointer-events-none', true); res(v); }, 200);
        };
        const onKey = (e) => { if (e.key === 'Enter') cleanup(true); if (e.key === 'Escape') cleanup(false); };
        window.addEventListener('keydown', onKey);
        $('confirm-ok-btn').onclick = () => cleanup(true);
        $('confirm-cancel-btn').onclick = () => cleanup(false);
    });
}

function toggleDeviceFields() {
    const isWifi = document.querySelector('input[name="dev-type"]:checked').value === 'WiFi';
    toggle('fields-wifi', isWifi);
    toggle('fields-zigbee', !isWifi);
}

function submitDeviceForm(e) {
    e.preventDefault();
    const payload = { id: $('dev-id').value, name: $('dev-name').value };
    if (document.querySelector('input[name="dev-type"]:checked').value === 'WiFi') {
        payload.key = $('dev-key').value;
        payload.ip = $('dev-ip').value || 'Auto';
        payload.version = $('dev-version').value || 'Auto';
    } else {
        payload.cid = $('dev-node').value;
        payload.parent_id = $('dev-parent').value;
    }
    sendCommand('add', payload);
    closeModal();
    closeDetails();
}

function populateParentsSelect() {
    const s = $('dev-parent'), prev = s.value;
    s.innerHTML = '<option value="">Select Parent...</option>';
    Object.values(devices_map).filter(d => !d.parent && !d.sub && !d.parent_id && !d.cid && d.status !== 'subdevice').forEach(d => {
        const o = document.createElement('option'); o.value = d.id; o.text = `${d.name || 'Unnamed'} (${d.id})`; s.appendChild(o);
    });
    if (Array.from(s.options).some(o => o.value === prev)) s.value = prev;
}

// =============================================================================
// Details panel
// =============================================================================
function toggleMaskedField(uid) {
    _maskedKeyRevealState[uid] = !_maskedKeyRevealState[uid];
    const valEl = $(`masked-val-${uid}`), iconEl = $(`masked-icon-${uid}`);
    if (!valEl || !iconEl) return;
    if (_maskedKeyRevealState[uid]) {
        valEl.textContent = valEl.dataset.raw;
        valEl.classList.add('font-mono', 'text-amber-300');
        iconEl.classList.replace('fa-eye', 'fa-eye-slash');
    } else {
        valEl.textContent = '••••••••••••••••';
        valEl.classList.remove('font-mono', 'text-amber-300');
        iconEl.classList.replace('fa-eye-slash', 'fa-eye');
    }
}

function renderDetailRow(key, val) {
    if (MASKED_DETAIL_KEYS.has(key)) {
        const uid = `${key}_${Math.random().toString(36).slice(2, 8)}`;
        _maskedKeyRevealState[uid] = false;
        return `<div class="detail-item"><span class="detail-label">${key.toUpperCase()}</span><span class="detail-value"><span id="masked-val-${uid}" data-raw="${val}" class="detail-value text-slate-500 tracking-widest">••••••••••••••••</span><button onclick="toggleMaskedField('${uid}')" class="text-slate-500 hover:text-slate-200 shrink-0 p-0.5"><i id="masked-icon-${uid}" class="fa-solid fa-eye text-sm"></i></button></span></div>`;
    }
    return `<div class="detail-item"><span class="detail-label">${key.toUpperCase()}</span><span class="detail-value" title="${val}">${val}</span></div>`;
}

function updateDetailsLiveValues(id) {
    const liveSec = $('live-values-section'), errSec = $('device-error-section');
    const dev = devices_map[id], online = dev && (dev.status === 'online' || dev.status === true || dev.status === '0' || dev.status === 0);
    const err = deviceErrors[id] || (!online && typeof dev?.status === 'string' && /^\d+$/.test(dev.status) ? `Error Code: ${dev.status}` : null);

    if (err) {
        toggle(liveSec, false); toggle(errSec, true);
        setHTML('device-error-body', `<div class="text-red-300 text-xs flex items-center gap-2"><i class="fa-solid fa-triangle-exclamation text-red-500"></i>${err}</div>`);
        return;
    }

    toggle(errSec, false);
    const vals = liveValues[id];
    if (!vals || !Object.keys(vals).length) { toggle(liveSec, false); return; }
    toggle(liveSec, true);
    setHTML('live-values-body', Object.entries(vals).map(([dp, { value, ts }]) =>
        `<div class="flex justify-between items-center text-xs py-1.5 border-b border-slate-700/50 last:border-0 gap-2"><span class="text-slate-400 font-mono shrink-0">${dp}</span><span class="text-emerald-400 font-semibold">${JSON.stringify(value)}</span><span class="text-slate-600 font-mono text-[10px] shrink-0">${ts}</span></div>`
    ).join(''));
}

function openDetails(id) {
    const panel = $('details-panel');
    if (currentDeviceId === id && !panel.classList.contains('translate-x-full')) { closeDetails(); return; }
    const dev = devices_map[id];
    if (!dev) return;
    currentDeviceId = id;

    let html = Object.entries(dev).filter(([k, v]) => !HIDDEN_DETAIL_KEYS.has(k) && v !== null && v !== undefined && typeof v !== 'object').map(([k, v]) => renderDetailRow(k, v)).join('');
    if (dev._missing_parent) html += `<div class="detail-item bg-amber-500/10 border border-amber-500/20 rounded p-2 mt-2"><span class="detail-label text-amber-500"><i class="fa-solid fa-link-slash mr-1"></i> MISSING PARENT</span><span class="detail-value font-mono text-amber-200">${dev._missing_parent}</span></div>`;
    if (dev._missing_parent && cloud_devices[dev._missing_parent]) html += `<div class="detail-item bg-blue-500/10 border border-blue-500/20 rounded p-2 mt-1"><span class="detail-label text-blue-400"><i class="fa-solid fa-cloud mr-1"></i> PARENT IN CLOUD</span><span class="detail-value text-slate-300">${cloud_devices[dev._missing_parent].name || dev._missing_parent} <span class="font-mono text-xs text-slate-500">(${dev._missing_parent})</span></span></div>`;

    setHTML('details-content', html);
    updateDetailsLiveValues(id);
    $('btn-edit').onclick = () => { closeDetails(); openEditDeviceModal(id); };
    $('btn-delete').onclick = async () => {
        if (await showConfirm({ title: 'Delete Device', message: `Delete ${dev.name || id}?`, okText: 'Delete' })) {
            sendCommand('remove', { id });
            if (devices_map[id]) { delete devices_map[id]; updateSyncStateAndRender(); }
            closeDetails();
        }
    };
    toggleClass('details-panel', 'translate-x-full', false);
    showPanelBackdrop();
    toggleClass('log-panel', 'translate-x-full', true);
}

function closeDetails() { toggleClass('details-panel', 'translate-x-full', true); currentDeviceId = null; hidePanelBackdrop(); }
function closeAllSidePanels() { closeDetails(); closeLogPanel(); }
function showPanelBackdrop() { const b = $('panel-backdrop'); if (b) { toggle(b, true); setTimeout(() => toggleClass(b, 'opacity-0', false), 10); } }
function hidePanelBackdrop() { const b = $('panel-backdrop'); if (b) { toggleClass(b, 'opacity-0', true); setTimeout(() => toggle(b, false), 300); } }

function toggleSidebar() {
    const s = $('sidebar'), b = $('sidebar-backdrop'), open = s.classList.contains('-translate-x-full');
    toggleClass(s, '-translate-x-full', !open);
    toggle(b, open);
}
function closeSidebar() { toggleClass('sidebar', '-translate-x-full', true); toggle('sidebar-backdrop', false); }

function showSection(id, e) {
    document.querySelectorAll('.section').forEach(s => toggle(s, false));
    toggle(id, true);
    document.querySelectorAll('.nav-item').forEach(n => toggleClass(n, 'active', false));
    if (e?.currentTarget) toggleClass(e.currentTarget, 'active', true);
    const titles = { dashboard: 'Device Dashboard', devices: 'Topology View', settings: 'Settings' };
    setText('page-title', titles[id] || 'Dashboard');
    if (window.innerWidth < 1024) closeSidebar();
}

function startWizard() {
    const code = $('wizard-code').value;
    if (!code) { showToast('Please enter a User Code.', 'warning'); return; }
    toggle('wizard-input-step', false); toggle('wizard-loading-step', true); toggle('wizard-spinner', true);
    setText('wizard-status-title', 'Starting API Login...'); setText('wizard-status-msg', 'Please wait.');
    sendCommand('wizard_start', { user_code: code });
}

function handleWizardEvent(status) {
    if (status.error) {
        setHTML('wizard-status-title', `<span class='text-red-500'>Error</span>`);
        setText('wizard-status-msg', status.error);
        toggle('wizard-spinner', false); toggle('wizard-qr-container', false);
        showToast(`Wizard failed: ${status.error}`, 'error', 6000);
        return;
    }
    setText('wizard-status-title', status.step);
    const hasQr = !!status.url;
    toggle('wizard-qr-container', hasQr);
    toggleClass('wizard-qr-container', 'flex', hasQr);
    toggle('wizard-spinner', !hasQr);
    if (hasQr) {
        try {
            const qr = qrcode(0, 'L'); qr.addData(status.url); qr.make();
            $('wizard-qr-img').src = qr.createDataURL(6);
        } catch (err) { console.error('QR Gen Error:', err); showToast('Local QR generation failed.', 'error'); }
        return;
    }
    if (!status.running) {
        toggle('wizard-spinner', false);
        showToast('Wizard complete! Cloud devices refreshed.', 'success', 4000);
        setTimeout(() => { closeModal(); requestStatusUpdate(); }, 1500);
    }
}

function requestSyncCheck() { requestStatusUpdate(); }
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') { closeModal(); closeDetails(); closeLogPanel(); } });
connectWS();
