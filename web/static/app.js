// =============================================================================
// State
// =============================================================================
let ws = null;
let reconnectTimer = null;
let devices_map = {};
let cloud_devices = {};
let currentSyncData = { missing: [], mismatched: [], orphaned: [], synced: [] };
let currentDeviceId = null;

// =============================================================================
// WebSocket
// =============================================================================
function connectWS() {
    ws = new WebSocket(`ws://${window.location.host}/ws`);

    ws.onopen = () => {
        clearTimeout(reconnectTimer);
        updateConnectionStatus(true);
        requestStatusUpdate();
    };

    ws.onmessage = ({ data }) => {
        const msg = JSON.parse(data);
        if (msg.type === 'wizard') {
            handleWizardEvent(msg.status);
            return;
        }
        if (msg.type === 'init' || msg.devices_updated || msg.type === 'status') {
            if (msg.cloud_devices) cloud_devices = msg.cloud_devices;
            if (msg.devices)       devices_map   = msg.devices;
            if (msg.type === 'init' && msg.user_code) {
                const el = document.getElementById('wizard-code');
                if (el) el.value = msg.user_code;
            }
            updateSyncStateAndRender();
        }
    };

    ws.onclose = () => {
        updateConnectionStatus(false);
        reconnectTimer = setTimeout(connectWS, 5000);
    };

    ws.onerror = () => ws.close();
}

function sendCommand(action, payload = {}) {
    if (ws?.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ action, payload }));
    } else {
        alert('Cannot send command, disconnected from backend');
    }
}

function updateConnectionStatus(connected) {
    const el = document.getElementById('connection-status');
    el.innerHTML = connected
        ? `<span class="status-dot status-online"></span>
           <span class="text-slate-300 font-medium tracking-wide">Connected</span>`
        : `<span class="relative flex h-3 w-3 mr-3">
               <span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-red-400 opacity-75"></span>
               <span class="relative inline-flex rounded-full h-3 w-3 bg-red-500"></span>
           </span>
           <span class="text-slate-300 font-medium tracking-wide">Disconnected</span>`;
}

// =============================================================================
// Sync state computation
// =============================================================================
function computeSyncState() {
    const result = { missing: [], mismatched: [], orphaned: [], synced: [] };
    const bridge_ids = Object.keys(devices_map);
    const cloud_ids  = Object.keys(cloud_devices);

    if (!cloud_ids.length) {
        result.synced = Object.values(devices_map);
        return result;
    }

    for (const cid of cloud_ids) {
        const cdev = cloud_devices[cid];
        if (!bridge_ids.includes(cid)) {
            result.missing.push(cdev);
            continue;
        }
        const bdev = devices_map[cid];
        const diff = [];
        if (cdev.name && bdev.name && cdev.name !== bdev.name) diff.push('name');
        const ckey = cdev.key || cdev.local_key || cdev.localkey;
        const bkey = bdev.key || bdev.local_key;
        if (ckey && bkey && ckey !== bkey) diff.push('local_key');
        if (diff.length > 0) {
            result.mismatched.push({ cloud: cdev, bridge: bdev, reasons: diff });
        } else {
            result.synced.push(bdev);
        }
    }

    for (const bid of bridge_ids) {
        if (!cloud_ids.includes(bid)) result.orphaned.push(devices_map[bid]);
    }
    return result;
}

function updateSyncStateAndRender() {
    currentSyncData = computeSyncState();
    updateStatsCards();
    renderSyncPanels();
    renderDashboard();
}

// =============================================================================
// Stats
// =============================================================================
function updateStatsCards() {
    document.getElementById('stat-total').innerText     = Object.keys(devices_map).length;
    document.getElementById('stat-cloud').innerText     = Object.keys(cloud_devices).length;
    document.getElementById('stat-synced').innerText    = currentSyncData.synced.length;
    document.getElementById('stat-conflicts').innerText = currentSyncData.missing.length + currentSyncData.mismatched.length;
}

function requestStatusUpdate() {
    sendCommand('status');
    const icon = document.querySelector('.fa-rotate-right');
    if (icon) {
        icon.classList.add('fa-spin');
        setTimeout(() => icon.classList.remove('fa-spin'), 1000);
    }
}

// =============================================================================
// Sync panels
// =============================================================================
/** Build an item row for sync panels */
function syncItemRow(label, id, btnText, btnClass, onClickExpr) {
    return `<div class="flex justify-between items-center text-sm border-b border-slate-700/50 pb-2 mb-2 last:border-0">
        <span class="text-white">${label}</span>
        <button onclick="${onClickExpr}" class="${btnClass} px-3 py-1.5 rounded transition-colors border text-sm font-medium">${btnText}</button>
    </div>`;
}

function renderSyncSection({ countId, sectionId, bodyId, items, renderRow }) {
    document.getElementById(countId).innerText = items.length;
    document.getElementById(sectionId).classList.toggle('hidden', items.length === 0);
    document.getElementById(bodyId).innerHTML = items.map(renderRow).join('');
}

function renderSyncPanels() {
    renderSyncSection({
        countId: 'count-missing', sectionId: 'section-missing', bodyId: 'body-missing',
        items: currentSyncData.missing,
        renderRow: (dev) => syncItemRow(
            `${dev.name} <span class="text-slate-500 font-mono text-xs">(${dev.id})</span>`,
            dev.id,
            'Import to Bridge',
            'text-rose-400 hover:text-white bg-rose-500/10 hover:bg-rose-500/30 border-rose-500/20',
            `resolveSingle('missing', '${dev.id}', event)`
        ),
    });

    renderSyncSection({
        countId: 'count-mismatch', sectionId: 'section-mismatch', bodyId: 'body-mismatch',
        items: currentSyncData.mismatched,
        renderRow: ({ cloud: dev, reasons }) => `
            <div class="flex flex-col text-sm border-b border-slate-700/50 pb-2 mb-2 last:border-0">
                <div class="flex justify-between items-center">
                    <span class="text-white">${dev.name} <span class="text-slate-500 font-mono text-xs">(${dev.id})</span></span>
                    <button onclick="resolveSingle('mismatched', '${dev.id}', event)"
                        class="text-amber-400 hover:text-white bg-amber-500/10 hover:bg-amber-500/30 border border-amber-500/20 px-3 py-1.5 rounded transition-colors text-sm font-medium">
                        Push Config to Bridge
                    </button>
                </div>
                <div class="text-xs text-slate-400 mt-1"><span class="text-amber-400">Conflicts:</span> ${reasons.join(', ')}</div>
            </div>`,
    });

    renderSyncSection({
        countId: 'count-orphan', sectionId: 'section-orphan', bodyId: 'body-orphan',
        items: currentSyncData.orphaned,
        renderRow: (dev) => syncItemRow(
            `${dev.name} <span class="text-slate-500 font-mono text-xs">(${dev.id})</span>`,
            dev.id,
            'Delete from Bridge',
            'text-slate-400 hover:text-red-400 bg-slate-700 hover:bg-red-500/20 border-slate-600 hover:border-red-500/30',
            `resolveSingle('orphaned', '${dev.id}', event)`
        ),
    });

    document.getElementById('count-synced').innerText = currentSyncData.synced.length;
}

// =============================================================================
// Resolve actions (DRY)
// =============================================================================
function resolveAll(category, e) {
    if (e) e.stopPropagation();
    const actions = {
        missing:    (dev) => submitDeviceBridgeAdd(dev),
        mismatched: (item) => submitDeviceBridgeAdd(item.cloud),
        orphaned:   (dev) => sendCommand('remove', { id: dev.id }),
    };
    currentSyncData[category].forEach(actions[category]);
}

function resolveSingle(category, id, e) {
    if (e) e.stopPropagation();
    const item = currentSyncData[category].find(
        x => (x.id ?? x.cloud?.id) === id
    );
    if (!item) return;
    if (category === 'orphaned') {
        sendCommand('remove', { id: item.id });
    } else {
        submitDeviceBridgeAdd(category === 'mismatched' ? item.cloud : item);
    }
}

// Keep old names as pass-throughs for HTML onclick attributes
const resolveMissing    = (e) => resolveAll('missing', e);
const resolveMismatched = (e) => resolveAll('mismatched', e);
const resolveOrphans    = (e) => resolveAll('orphaned', e);

function isPrivateIP(ip) {
    if (!ip || typeof ip !== 'string') return false;
    if (ip.toLowerCase() === 'auto') return true;
    const [a, b, c] = ip.split('.');
    if (a === '10') return true;
    if (a === '192' && b === '168') return true;
    if (a === '172' && +b >= 16 && +b <= 31) return true;
    return false;
}

function submitDeviceBridgeAdd(dev) {
    const payload = { id: dev.id, name: dev.name || 'Unnamed' };
    if (dev.sub || dev.parent || dev.parent_id) {
        payload.node_id = dev.node_id || dev.cid;
        payload.parent  = dev.parent  || dev.parent_id;
    } else {
        const ip = isPrivateIP(dev.ip) ? dev.ip : 'Auto';
        Object.assign(payload, {
            key:     dev.key || dev.local_key || dev.localkey,
            ip,
            version: dev.version || '3.3',
        });
    }
    sendCommand('add', payload);
}

// =============================================================================
// Tree helpers (DRY – shared by renderDashboard + renderTree)
// =============================================================================
function buildTree(devices) {
    const rootNodes = [];
    const childMap  = {};
    for (const d of devices) {
        const parentId = d.parent || d.parent_id;
        if (parentId) {
            (childMap[parentId] ??= []).push(d);
        } else {
            rootNodes.push(d);
        }
    }
    return { rootNodes, childMap };
}

// =============================================================================
// Dashboard rendering
// =============================================================================
function renderStatusCell(dev) {
    const isSubDevice = ['subdevice', 'no parent', 'invalid subdevice'].includes(dev.status);
    if (isSubDevice) {
        return `<span class="status-dot" style="background:#3b82f6;box-shadow:0 0 0 2px rgba(59,130,246,0.25)"></span>
                <span class="text-sm font-medium text-blue-400">Sub-device</span>`;
    }
    const online = dev.status === undefined || dev.status === 'online' || dev.status === true;
    return `<span class="status-dot ${online ? 'status-online' : 'status-offline'}"></span>
            <span class="text-sm font-medium ${online ? 'text-slate-300' : 'text-slate-500'}">${online ? 'Online' : 'Offline'}</span>`;
}

function renderDashboard() {
    const tbody  = document.getElementById('devices-body');
    const search = document.getElementById('search-input').value.toLowerCase();
    tbody.innerHTML = '';

    const devices = currentSyncData.synced.length > 0
        ? currentSyncData.synced
        : Object.values(devices_map);

    if (!devices.length) {
        tbody.innerHTML = `<tr><td colspan="5" class="py-12 text-center text-slate-500">No strictly synced devices found.</td></tr>`;
        return;
    }

    const { rootNodes, childMap } = buildTree(devices);

    function appendRow(dev, indent) {
        const matches = !search
            || (dev.name || '').toLowerCase().includes(search)
            || (dev.id   || '').toLowerCase().includes(search);
        if (!matches) return;

        const isZigbee  = !!(dev.sub || dev.parent || dev.parent_id);
        const typeStr   = isZigbee ? 'Zigbee/BLE' : 'WiFi';
        const iconType  = isZigbee ? 'fa-network-wired' : 'fa-wifi';
        const indentPx  = search ? 0 : indent * 2;
        const indentIcon = (indent > 0 && !search)
            ? `<i class="fa-solid fa-level-up-alt fa-rotate-90 text-slate-600 mr-2 opacity-70"></i>`
            : '';

        const tr = document.createElement('tr');
        tr.onclick   = () => openDetails(dev.id);
        tr.className = 'border-b border-slate-700/50 hover:bg-slate-800/40 transition-colors cursor-pointer group';
        tr.innerHTML = `
            <td class="py-4 px-5">
                <div class="flex items-center" style="padding-left:${indentPx}rem">
                    ${indentIcon}${renderStatusCell(dev)}
                </div>
            </td>
            <td class="py-4 px-5">
                <div class="flex items-center text-slate-400 group-hover:text-brandBlue transition-colors">
                    <i class="fa-solid ${iconType} mr-2 w-4 text-center"></i>
                    <span class="text-sm font-medium">${typeStr}</span>
                </div>
            </td>
            <td class="py-4 px-5 text-sm font-medium text-white">${dev.name || 'Unnamed Device'}</td>
            <td class="py-4 px-5 font-mono text-xs text-slate-400 group-hover:text-slate-300 transition-colors">${dev.id}</td>
            <td class="py-4 px-5 text-right">
                <button class="text-slate-500 hover:text-white p-2 rounded hover:bg-slate-700 transition-colors"
                        onclick="event.stopPropagation(); openDetails('${dev.id}')">
                    <i class="fa-solid fa-chevron-right"></i>
                </button>
            </td>`;
        tbody.appendChild(tr);

        if (!search) (childMap[dev.id] || []).forEach(c => appendRow(c, indent + 1));
    }

    const roots = (rootNodes.length === 0 && devices.length > 0) ? devices : rootNodes;
    if (search) {
        devices.forEach(d => appendRow(d, 0));
    } else {
        roots.forEach(d => appendRow(d, 0));
    }

    populateParentsSelect();
}

// =============================================================================
// Tree view (topology)
// =============================================================================
function renderTree() {
    const container = document.getElementById('tree-container');
    if (!container) return;
    container.innerHTML = '';

    const { rootNodes, childMap } = buildTree(Object.values(devices_map));

    function createNode(dev) {
        const wrapper  = document.createElement('div');
        const isOnline = dev.status === 'online';
        const iconType = dev.parent ? 'fa-microchip' : 'fa-network-wired';
        const color    = isOnline ? 'text-green-400' : 'text-slate-500';
        wrapper.innerHTML = `
            <div class="tree-node" onclick="openDetails('${dev.id}')">
                <i class="fa-solid ${iconType} ${color} w-6 text-center mr-2"></i>
                <div>
                    <div class="text-sm font-medium text-white">${dev.name || 'Unnamed'}</div>
                    <div class="text-xs text-slate-500 font-mono">${dev.id}</div>
                </div>
            </div>`;
        const children = childMap[dev.id] || [];
        if (children.length) {
            const childContainer = document.createElement('div');
            childContainer.className = 'tree-children';
            children.forEach(c => childContainer.appendChild(createNode(c)));
            wrapper.appendChild(childContainer);
        }
        return wrapper;
    }

    const roots = (rootNodes.length === 0 && Object.keys(devices_map).length > 0)
        ? Object.values(devices_map)
        : rootNodes;
    roots.forEach(d => container.appendChild(createNode(d)));
}

// =============================================================================
// Modals
// =============================================================================
function openAddDeviceModal() {
    currentDeviceId = null;
    document.getElementById('modal-title').innerText = 'Add Device';
    document.getElementById('device-form').reset();
    document.getElementById('dev-id').readOnly = false;
    toggleDeviceFields();
    showModal('device-modal');
}

function openEditDeviceModal(id) {
    const dev = devices_map[id];
    if (!dev) return;
    currentDeviceId = id;
    document.getElementById('modal-title').innerText = 'Edit Device';
    document.getElementById('dev-id').value    = id;
    document.getElementById('dev-id').readOnly = true;
    document.getElementById('dev-name').value  = dev.name || '';

    const isZigbee = !!(dev.parent || dev.sub || dev.node_id);
    document.querySelector(`input[name="dev-type"][value="${isZigbee ? 'Zigbee/BLE' : 'WiFi'}"]`).checked = true;
    toggleDeviceFields();

    if (isZigbee) {
        document.getElementById('dev-node').value   = dev.node_id || dev.cid || '';
        document.getElementById('dev-parent').value = dev.parent || '';
    } else {
        document.getElementById('dev-key').value     = dev.key || dev.local_key || '';
        document.getElementById('dev-ip').value      = dev.ip  || '';
        document.getElementById('dev-version').value = dev.version || '';
    }
    showModal('device-modal');
}

function openWizardModal() {
    document.getElementById('device-modal').classList.add('hidden');
    document.getElementById('wizard-modal').classList.remove('hidden');
    document.getElementById('wizard-input-step').classList.remove('hidden');
    document.getElementById('wizard-loading-step').classList.add('hidden');
    document.getElementById('wizard-qr-container').classList.add('hidden');
    document.getElementById('wizard-spinner').classList.add('hidden');
    showModal('wizard-modal');
}

function showModal(modalId) {
    const overlay = document.getElementById('modal-overlay');
    overlay.classList.remove('opacity-0', 'pointer-events-none');
    document.getElementById(modalId).classList.remove('scale-95', 'hidden');
}

function closeModal() {
    const overlay = document.getElementById('modal-overlay');
    overlay.classList.add('opacity-0', 'pointer-events-none');
    ['device-modal', 'wizard-modal', 'sync-modal'].forEach(id => {
        const el = document.getElementById(id);
        el.classList.add('scale-95');
        setTimeout(() => el.classList.add('hidden'), 300);
    });
}

function toggleDeviceFields() {
    const isWifi = document.querySelector('input[name="dev-type"]:checked').value === 'WiFi';
    document.getElementById('fields-wifi').classList.toggle('hidden', !isWifi);
    document.getElementById('fields-zigbee').classList.toggle('hidden', isWifi);
}

function submitDeviceForm(e) {
    e.preventDefault();
    const payload = {
        id:   document.getElementById('dev-id').value,
        name: document.getElementById('dev-name').value,
    };
    if (document.querySelector('input[name="dev-type"]:checked').value === 'WiFi') {
        payload.key     = document.getElementById('dev-key').value;
        payload.ip      = document.getElementById('dev-ip').value || 'Auto';
        payload.version = document.getElementById('dev-version').value || 'Auto';
    } else {
        payload.cid       = document.getElementById('dev-node').value;
        payload.parent_id = document.getElementById('dev-parent').value;
    }
    sendCommand('add', payload);
    closeModal();
    closeDetails();
}

function populateParentsSelect() {
    const select  = document.getElementById('dev-parent');
    const prevVal = select.value;
    select.innerHTML = '<option value="">Select Parent...</option>';
    Object.values(devices_map)
        .filter(d => !d.parent && !d.sub)
        .forEach(d => {
            const opt = document.createElement('option');
            opt.value = d.id;
            opt.text  = `${d.name || 'Unnamed'} (${d.id})`;
            select.appendChild(opt);
        });
    if (Array.from(select.options).some(o => o.value === prevVal)) select.value = prevVal;
}

// =============================================================================
// Details panel
// =============================================================================
function renderDetailRow(key, val) {
    return `<div class="detail-item">
        <span class="detail-label">${key.toUpperCase()}</span>
        <span class="detail-value" title="${val}">${val}</span>
    </div>`;
}

function openDetails(id) {
    const panel = document.getElementById('details-panel');
    if (currentDeviceId === id && !panel.classList.contains('translate-x-full')) {
        closeDetails();
        return;
    }
    const dev = devices_map[id];
    if (!dev) return;
    currentDeviceId = id;

    document.getElementById('details-content').innerHTML = Object.entries(dev)
        .filter(([, v]) => v !== null && v !== undefined && typeof v !== 'object')
        .map(([k, v]) => renderDetailRow(k, v))
        .join('');

    document.getElementById('btn-edit').onclick   = () => { closeDetails(); openEditDeviceModal(id); };
    document.getElementById('btn-delete').onclick = () => {
        if (confirm(`Are you sure you want to delete ${dev.name || id}?`)) {
            sendCommand('delete', { id });
            closeDetails();
        }
    };

    panel.classList.remove('translate-x-full');
}

function closeDetails() {
    document.getElementById('details-panel').classList.add('translate-x-full');
}

// =============================================================================
// Navigation
// =============================================================================
function showSection(sectionId) {
    document.querySelectorAll('.section').forEach(s => s.classList.add('hidden'));
    document.getElementById(sectionId).classList.remove('hidden');
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    event.currentTarget.classList.add('active');
    const titles = { dashboard: 'Device Dashboard', devices: 'Topology View', settings: 'Settings' };
    document.getElementById('page-title').innerText = titles[sectionId] || 'Dashboard';
}

// =============================================================================
// Wizard
// =============================================================================
function startWizard() {
    const code = document.getElementById('wizard-code').value;
    if (!code) { alert('Please enter a User Code.'); return; }
    document.getElementById('wizard-input-step').classList.add('hidden');
    document.getElementById('wizard-loading-step').classList.remove('hidden');
    document.getElementById('wizard-spinner').classList.remove('hidden');
    document.getElementById('wizard-status-title').innerText = 'Starting API Login...';
    document.getElementById('wizard-status-msg').innerText   = 'Please wait.';
    sendCommand('wizard_start', { user_code: code });
}

function handleWizardEvent(status) {
    if (status.error) {
        document.getElementById('wizard-status-title').innerHTML = `<span class='text-red-500'>Error</span>`;
        document.getElementById('wizard-status-msg').innerText   = status.error;
        document.getElementById('wizard-spinner').classList.add('hidden');
        document.getElementById('wizard-qr-container').classList.add('hidden');
        return;
    }

    document.getElementById('wizard-status-title').innerText = status.step;

    const hasQr     = !!status.url;
    const qrEl      = document.getElementById('wizard-qr-container');
    const spinnerEl = document.getElementById('wizard-spinner');

    qrEl.classList.toggle('hidden', !hasQr);
    qrEl.classList.toggle('flex', hasQr);
    spinnerEl.classList.toggle('hidden', hasQr);

    if (hasQr) {
        document.getElementById('wizard-qr-img').src =
            `https://api.qrserver.com/v1/create-qr-code/?size=250x250&data=${encodeURIComponent(status.url)}`;
        return;
    }

    if (!status.running) {
        spinnerEl.classList.add('hidden');
        setTimeout(() => { closeModal(); requestStatusUpdate(); }, 1500);
    }
}

// =============================================================================
// Sync modal refresh — alias for status update
function requestSyncCheck() { requestStatusUpdate(); }

// =============================================================================
// Bootstrap
// =============================================================================
connectWS();
