// =============================================================================
// State
// =============================================================================
let ws = null;
let reconnectTimer = null;
let devices_map = {};
let cloud_devices = {};
let currentSyncData = { missing: [], mismatched: [], orphaned: [], synced: [] };
let currentDeviceId = null;
let currentFilter = 'all';          // 'all' | 'online' | 'offline' | 'subdevice'
let liveValues = {};                // { [device_id]: { [dp]: value } }
let deviceErrors = {};              // { [device_id]: "error message" }
let activityLog = [];               // ring buffer, max 100 entries
const MAX_LOG = 100;

// =============================================================================
// Toast Notification System
// =============================================================================
function showToast(message, level = 'info', duration = 3500) {
    const container = document.getElementById('toast-container');
    if (!container) return;

    // Limit maximum toasts on screen
    const MAX_TOASTS = 5;
    while (container.children.length >= MAX_TOASTS) {
        container.children[0].remove();
    }

    const colors = {
        success: 'bg-emerald-600 border-emerald-500',
        error:   'bg-red-700    border-red-600',
        info:    'bg-slate-700  border-slate-600',
        warning: 'bg-amber-600  border-amber-500',
    };
    const icons = {
        success: 'fa-circle-check',
        error:   'fa-circle-xmark',
        info:    'fa-circle-info',
        warning: 'fa-triangle-exclamation',
    };

    const toast = document.createElement('div');
    toast.className = `flex items-start gap-3 px-4 py-3 rounded-lg border shadow-2xl text-white text-sm
                       transition-all duration-300 opacity-0 -translate-y-4 max-w-md w-auto
                       ${colors[level] ?? colors.info}`;
    toast.innerHTML = `<i class="fa-solid ${icons[level] ?? icons.info} mt-0.5 flex-shrink-0"></i>
                       <span class="break-words overflow-hidden">${message}</span>`;
    container.appendChild(toast);

    requestAnimationFrame(() => {
        toast.classList.remove('opacity-0', '-translate-y-4');
    });

    setTimeout(() => {
        toast.classList.add('opacity-0', '-translate-y-4');
        setTimeout(() => toast.remove(), 300);
    }, duration);
}

// =============================================================================
// Activity Log
// =============================================================================
function addLog(message, level = 'info') {
    const entry = { ts: new Date().toLocaleTimeString(), message, level };
    activityLog.unshift(entry);
    if (activityLog.length > MAX_LOG) activityLog.pop();
    renderActivityLog();
}

function renderActivityLog() {
    const el = document.getElementById('log-body');
    if (!el) return;
    const colors = { info: 'text-slate-300', error: 'text-red-400', success: 'text-emerald-400', warning: 'text-amber-400' };
    el.innerHTML = activityLog.map(({ ts, message, level }) =>
        `<div class="flex gap-3 text-xs py-1.5 border-b border-slate-700/50 last:border-0">
            <span class="text-slate-500 shrink-0 font-mono">${ts}</span>
            <span class="${colors[level] ?? colors.info}">${message}</span>
         </div>`
    ).join('') || `<p class="text-slate-500 text-sm text-center py-6">No activity yet.</p>`;
}

function toggleLogPanel() {
    document.getElementById('log-panel').classList.toggle('translate-x-full');
    
    // Toggle backdrop
    const isVisible = !document.getElementById('log-panel').classList.contains('translate-x-full');
    if (isVisible) {
        const backdrop = document.getElementById('panel-backdrop');
        if (backdrop) {
            backdrop.classList.remove('hidden');
            setTimeout(() => backdrop.classList.remove('opacity-0'), 10);
        }
    } else {
        hidePanelBackdrop();
    }

    // Close details if open
    document.getElementById('details-panel').classList.add('translate-x-full');
}

function closeLogPanel() {
    document.getElementById('log-panel').classList.add('translate-x-full');
    hidePanelBackdrop();
}

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

        if (msg.type === 'mqtt_status') {
            updateMqttBrokerStatus(msg.connected);
            if (!msg.connected) showToast('MQTT broker disconnected', 'warning');
            else showToast('MQTT broker connected', 'success', 2000);
            return;
        }

        if (msg.type === 'wizard') {
            handleWizardEvent(msg.status);
            // Wizard completion may carry refreshed cloud_devices
            if (msg.cloud_devices) {
                cloud_devices = msg.cloud_devices;
                updateSyncStateAndRender();
            }
            return;
        }

        if (msg.type === 'bridge_response') {
            const level = msg.level === 'error' ? 'error' : 'success';
            showToast(msg.message, level);
            addLog(msg.message, level);
            return;
        }

        if (msg.type === 'mqtt') {
            // Bridge response/error via MQTT topic
            if (msg.topic_type === 'response') {
                const p = msg.payload || {};
                let text = p.message;
                if (!text) {
                    if (p.action === 'status') {
                        const count = Object.keys(p.devices || {}).length;
                        text = `Status updated (${count} devices)`;
                    } else if (p.action === 'remove') {
                        text = `Device removed: ${p.id}`;
                    } else {
                        text = JSON.stringify(p);
                    }
                }
                showToast(`Bridge: ${text}`, 'success');
                addLog(`Bridge OK — ${text}`, 'success');
            } else if (msg.topic_type === 'error') {
                const p = msg.payload || {};
                let errMsg = p.errorMsg || p.message || p.error;
                if (!errMsg) {
                    if (p.action === 'status') {
                        const count = Object.keys(p.devices || {}).length;
                        errMsg = `Status updated (${count} devices)`;
                    } else {
                        errMsg = JSON.stringify(p);
                    }
                }
                const namePart = p.name ? `[${p.name}] ` : '';
                const idPart   = p.id   ? `(${p.id}) ` : '';
                const text     = `${namePart}${idPart}${errMsg}`;

                const isRealError = p.errorCode !== 0 && p.status !== 'success';
                const level = isRealError ? 'error' : 'success';
                const prefix = isRealError ? 'Bridge ERR' : 'Bridge OK';

                // Suppress duplicate errors for the same device
                const isDuplicate = p.id && isRealError && deviceErrors[p.id] === text;

                if (!isDuplicate) {
                    showToast(`Bridge: ${text}`, level);
                    addLog(`${prefix} — ${text}`, level);
                }

                if (isRealError && p.id) {
                    deviceErrors[p.id] = text;
                } else if (p.errorCode === 0 && p.id) {
                    delete deviceErrors[p.id];
                }
            } else if (msg.topic_type === 'event') {
                // Accumulate live DPS values (exclude metadata keys)
                if (msg.payload?.action) return; // Ignore bridge responses mistakenly classified as events
                
                const META_KEYS = new Set(['id', 'name', 'cid']);
                const did = msg.payload?.id;
                if (did && msg.payload) {
                    const raw = msg.payload.dps || msg.payload;
                    if (typeof raw === 'object' && !Array.isArray(raw)) {
                        const ts = new Date().toLocaleTimeString();
                        liveValues[did] = { ...(liveValues[did] ?? {}) };
                        for (const [dp, v] of Object.entries(raw)) {
                            if (!META_KEYS.has(dp)) liveValues[did][dp] = { value: v, ts };
                        }
                        if (currentDeviceId === did) updateDetailsLiveValues(did);
                    }
                }
            }
        }

        if (msg.type === 'init' || msg.devices_updated || msg.type === 'status') {
            if (msg.cloud_devices) cloud_devices = msg.cloud_devices;
            if (msg.devices)       devices_map   = msg.devices;
            if (msg.mqtt_connected !== undefined) updateMqttBrokerStatus(msg.mqtt_connected);
            if (msg.type === 'init' && msg.user_code) {
                const el = document.getElementById('wizard-code');
                if (el) el.value = msg.user_code;
            }
            updateSyncStateAndRender();
        }
    };

    ws.onclose = () => {
        updateConnectionStatus(false);
        updateMqttBrokerStatus(false);
        reconnectTimer = setTimeout(connectWS, 5000);
    };

    ws.onerror = () => ws.close();
}

function sendCommand(action, payload = {}) {
    if (ws?.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ action, payload }));
        addLog(`→ ${action}${payload.id ? ` [${payload.id}]` : ''}`, 'info');
    } else {
        showToast('Disconnected from backend', 'error');
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

function updateMqttBrokerStatus(connected) {
    const el = document.getElementById('mqtt-broker-status');
    if (!el) return;
    el.innerHTML = connected
        ? `<span class="status-dot status-online"></span>
           <span class="text-slate-400 text-xs tracking-wide">MQTT Broker</span>`
        : `<span class="status-dot status-offline"></span>
           <span class="text-slate-400 text-xs tracking-wide">MQTT Broker</span>`;
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
        diff.length > 0
            ? result.mismatched.push({ cloud: cdev, bridge: bdev, reasons: diff })
            : result.synced.push(bdev);
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
function syncItemRow(label, id, btnText, btnClass, onClickExpr, onEditExpr) {
    return `<div class="flex justify-between items-center text-sm border-b border-slate-700/50 pb-2 mb-2 last:border-0">
        <span class="text-white">${label}</span>
        <div class="flex gap-2">
            ${onEditExpr ? `<button onclick="${onEditExpr}" title="Edit before adding" class="px-2 py-1.5 rounded bg-slate-700 hover:bg-slate-600 text-slate-400 hover:text-white transition-colors border border-slate-600"><i class="fa-solid fa-pen-to-square"></i></button>` : ''}
            <button onclick="${onClickExpr}" class="${btnClass} px-3 py-1.5 rounded transition-colors border text-sm font-medium">${btnText}</button>
        </div>
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
            dev.id, 'Import to Bridge',
            'text-emerald-400 hover:text-white bg-emerald-500/10 hover:bg-emerald-500/30 border-emerald-500/20',
            `resolveSingle('missing', '${dev.id}', event)`,
            `openDeviceImportModal(cloud_devices['${dev.id}'])`
        ),
    });

    renderSyncSection({
        countId: 'count-mismatch', sectionId: 'section-mismatch', bodyId: 'body-mismatch',
        items: currentSyncData.mismatched,
        renderRow: (item) => {
            const row = syncItemRow(
                `${item.cloud.name} <span class="text-slate-500 font-mono text-xs">(${item.id})</span>`,
                item.id, 'Push to Bridge',
                'text-amber-400 hover:text-white bg-amber-500/10 hover:bg-amber-500/30 border-amber-500/20',
                `resolveSingle('mismatched', '${item.id}', event)`,
                `openDeviceImportModal(cloud_devices['${item.id}'])`
            );
            return `<div class="mb-2 last:mb-0">${row}<div class="text-[10px] text-amber-500/70 -mt-1.5 mb-2 px-1">Conflicts: ${item.reasons.join(', ')}</div></div>`;
        }
    });

    renderSyncSection({
        countId: 'count-orphan', sectionId: 'section-orphan', bodyId: 'body-orphan',
        items: currentSyncData.orphaned,
        renderRow: (dev) => syncItemRow(
            `${dev.name} <span class="text-slate-500 font-mono text-xs">(${dev.id})</span>`,
            dev.id, 'Delete from Bridge',
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
        missing:    (dev)  => submitDeviceBridgeAdd(dev),
        mismatched: (item) => submitDeviceBridgeAdd(item.cloud),
        orphaned:   (dev)  => sendCommand('remove', { id: dev.id }),
    };
    currentSyncData[category].forEach(actions[category]);
}

function resolveSingle(category, id, e) {
    if (e) e.stopPropagation();
    const item = currentSyncData[category].find(x => (x.id ?? x.cloud?.id) === id);
    if (!item) return;
    category === 'orphaned'
        ? sendCommand('remove', { id: item.id })
        : submitDeviceBridgeAdd(category === 'mismatched' ? item.cloud : item);
}

// Legacy shims for HTML onclick
const resolveMissing    = (e) => resolveAll('missing', e);
const resolveMismatched = (e) => resolveAll('mismatched', e);
const resolveOrphans    = (e) => resolveAll('orphaned', e);

function isPrivateIP(ip) {
    if (!ip || typeof ip !== 'string') return false;
    if (ip.toLowerCase() === 'auto') return true;
    const [a, b] = ip.split('.');
    if (a === '10') return true;
    if (a === '192' && b === '168') return true;
    if (a === '172' && +b >= 16 && +b <= 31) return true;
    return false;
}

function submitDeviceBridgeAdd(dev) {
    const payload = { id: dev.id, name: dev.name || 'Unnamed' };
    // Sub-device check with standardized fields for the bridge (cid, parent_id)
    if (dev.sub || dev.parent || dev.parent_id || dev.node_id || dev.cid) {
        payload.cid       = dev.cid || dev.node_id;
        payload.parent_id = dev.parent_id || dev.parent;
    } else {
        Object.assign(payload, {
            key:     dev.key || dev.local_key || dev.localkey,
            ip:      isPrivateIP(dev.ip) ? dev.ip : 'Auto',
            version: dev.version || 'Auto',
        });
    }
    sendCommand('add', payload);
}

// =============================================================================
// Tree helper (shared by renderDashboard + renderTree)
// =============================================================================
function buildTree(devices) {
    const rootNodes = [];
    const childMap  = {};
    for (const d of devices) {
        const parentId = d.parent || d.parent_id;
        if (parentId) (childMap[parentId] ??= []).push(d);
        else           rootNodes.push(d);
    }
    return { rootNodes, childMap };
}

// =============================================================================
// Status filter
// =============================================================================
function setFilter(filter) {
    currentFilter = filter;
    document.querySelectorAll('.filter-btn').forEach(btn => {
        btn.classList.toggle('active-filter', btn.dataset.filter === filter);
    });
    renderDashboard();
}

function passesFilter(dev) {
    if (currentFilter === 'all') return true;
    const isSubDevice = ['subdevice', 'no parent', 'invalid subdevice'].includes(dev.status);
    if (currentFilter === 'subdevice') return isSubDevice;
    if (currentFilter === 'online')    return !isSubDevice && (dev.status === undefined || dev.status === 'online' || dev.status === true || dev.status === '0' || dev.status === 0);
    if (currentFilter === 'offline') {
        const isOffline = dev.status === 'offline' || (typeof dev.status === 'string' && /^\d+$/.test(dev.status) && dev.status !== '0');
        return !isSubDevice && isOffline;
    }
    return true;
}

// =============================================================================
// Dashboard rendering
// =============================================================================
function renderStatusCell(dev) {
    const errorMsg = deviceErrors[dev.id];
    const isSubDevice = ['subdevice', 'no parent', 'invalid subdevice'].includes(dev.status);
    const online = dev.status === 'online' || dev.status === true || dev.status === '0' || dev.status === 0;
    
    // Check if status is a non-zero error code
    const isErrorCode = !online && !isSubDevice && typeof dev.status === 'string' && /^\d+$/.test(dev.status);
    
    if (errorMsg || isErrorCode) {
        let text = 'Error';
        if (isErrorCode) text = `Error ${dev.status}`;
        
        return `<span class="status-dot bg-red-500 shadow-[0_0_8px_rgba(239,68,68,0.5)]"></span>
                <span class="text-sm font-medium text-red-500">${text}</span>`;
    }

    if (isSubDevice) {
        return `<span class="status-dot" style="background:#3b82f6;box-shadow:0 0 0 2px rgba(59,130,246,0.25)"></span>
                <span class="text-sm font-medium text-blue-400">Sub-device</span>`;
    }

    return `<span class="status-dot ${online ? 'status-online' : 'status-offline'}"></span>
            <span class="text-sm font-medium ${online ? 'text-slate-300' : 'text-slate-500'}">${online ? 'Online' : 'Offline'}</span>`;
}

function renderDashboard() {
    const tbody  = document.getElementById('devices-body');
    const search = document.getElementById('search-input').value.toLowerCase();
    tbody.innerHTML = '';

    const allDevices = currentSyncData.synced.length > 0
        ? currentSyncData.synced
        : Object.values(devices_map);
    const devices = allDevices.filter(d => passesFilter(d));

    if (!devices.length) {
        tbody.innerHTML = `<tr><td colspan="5" class="py-12 text-center text-slate-500">No devices match the current filter.</td></tr>`;
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

        const hasLive = !!liveValues[dev.id];

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
            <td class="py-4 px-5 text-sm font-medium text-white">
                ${dev.name || 'Unnamed Device'}
                ${deviceErrors[dev.id] ? `<span class="ml-2 text-xs text-red-500 font-normal animate-pulse">● error</span>` : (hasLive ? `<span class="ml-2 text-xs text-emerald-500 font-normal">● live</span>` : '')}
            </td>
            <td class="py-4 px-5 font-mono text-xs text-slate-400 group-hover:text-slate-300 transition-colors">${dev.id}</td>
            <td class="py-4 px-5 text-right">
                <button aria-label="Open device details"
                        class="text-slate-500 hover:text-white p-2 rounded hover:bg-slate-700 transition-colors"
                        onclick="event.stopPropagation(); openDetails('${dev.id}')">
                    <i class="fa-solid fa-chevron-right"></i>
                </button>
            </td>`;
        tbody.appendChild(tr);

        if (!search) (childMap[dev.id] || []).forEach(c => appendRow(c, indent + 1));
    }

    const roots = (rootNodes.length === 0 && devices.length > 0) ? devices : rootNodes;
    if (search) devices.forEach(d => appendRow(d, 0));
    else        roots.forEach(d => appendRow(d, 0));

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
        ? Object.values(devices_map) : rootNodes;
    roots.forEach(d => container.appendChild(createNode(d)));
}

// =============================================================================
// Modals
// =============================================================================
function showModal(modalId) {
    document.getElementById('modal-overlay').classList.remove('opacity-0', 'pointer-events-none');
    document.getElementById(modalId).classList.remove('scale-95', 'hidden');
}

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
        document.getElementById('dev-version').value = dev.version || 'Auto';
    }
    showModal('device-modal');
}

function openDeviceImportModal(dev) {
    currentDeviceId = null; // New device (no bridge ID yet)
    document.getElementById('modal-title').innerText = 'Import Device';
    document.getElementById('dev-id').value = dev.id;
    document.getElementById('dev-id').readOnly = true; // ID is fixed from cloud
    document.getElementById('dev-name').value = dev.name || '';

    const isZigbee = !!(dev.sub || dev.parent || dev.parent_id || dev.node_id || dev.cid);
    document.querySelector(`input[name="dev-type"][value="${isZigbee ? 'Zigbee/BLE' : 'WiFi'}"]`).checked = true;
    toggleDeviceFields();

    if (isZigbee) {
        document.getElementById('dev-node').value   = dev.cid || dev.node_id || '';
        document.getElementById('dev-parent').value = dev.parent_id || dev.parent || '';
    } else {
        document.getElementById('dev-key').value     = dev.key || dev.local_key || dev.localkey || '';
        document.getElementById('dev-ip').value      = isPrivateIP(dev.ip) ? dev.ip : '';
        document.getElementById('dev-version').value = dev.version || 'Auto';
        if (!document.getElementById('dev-ip').value) {
            document.getElementById('dev-ip').placeholder = 'Auto (Local Discovery)';
        }
    }
    showModal('device-modal');
}

function openWizardModal() {
    ['device-modal', 'sync-modal'].forEach(id => document.getElementById(id).classList.add('hidden'));
    document.getElementById('wizard-modal').classList.remove('hidden');
    document.getElementById('wizard-input-step').classList.remove('hidden');
    document.getElementById('wizard-loading-step').classList.add('hidden');
    document.getElementById('wizard-qr-container').classList.add('hidden');
    document.getElementById('wizard-spinner').classList.add('hidden');
    showModal('wizard-modal');
}

function closeModal() {
    document.getElementById('modal-overlay').classList.add('opacity-0', 'pointer-events-none');
    ['device-modal', 'wizard-modal', 'sync-modal', 'confirm-modal'].forEach(id => {
        const el = document.getElementById(id);
        el.classList.add('scale-95');
        setTimeout(() => el.classList.add('hidden'), 300);
    });
}

function showConfirm({ title = 'Are you sure?', message = '', okText = 'Confirm', cancelText = 'Cancel' }) {
    return new Promise((resolve) => {
        // Hide other modals to prevent overlap
        ['device-modal', 'wizard-modal', 'sync-modal'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.classList.add('hidden', 'scale-95');
        });

        const modal = document.getElementById('confirm-modal');
        const overlay = document.getElementById('modal-overlay');
        const titleEl = document.getElementById('confirm-title');
        const msgEl = document.getElementById('confirm-message');
        const okBtn = document.getElementById('confirm-ok-btn');
        const cancelBtn = document.getElementById('confirm-cancel-btn');

        titleEl.innerText = title;
        msgEl.innerText = message;
        okBtn.innerText = okText;
        cancelBtn.innerText = cancelText;

        overlay.classList.remove('opacity-0', 'pointer-events-none');
        modal.classList.remove('hidden');
        setTimeout(() => modal.classList.remove('scale-95'), 10);

        const onOk = () => { cleanup(true); };
        const onCancel = () => { cleanup(false); };

        const cleanup = (result) => {
            modal.classList.add('scale-95');
            setTimeout(() => {
                modal.classList.add('hidden');
                overlay.classList.add('opacity-0', 'pointer-events-none');
                resolve(result);
            }, 200);
            okBtn.removeEventListener('click', onOk);
            cancelBtn.removeEventListener('click', onCancel);
        };

        okBtn.addEventListener('click', onOk);
        cancelBtn.addEventListener('click', onCancel);
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
const HIDDEN_DETAIL_KEYS = new Set(['dps']);

function renderDetailRow(key, val) {
    return `<div class="detail-item">
        <span class="detail-label">${key.toUpperCase()}</span>
        <span class="detail-value" title="${val}">${val}</span>
    </div>`;
}

function updateDetailsLiveValues(id) {
    const liveEl   = document.getElementById('live-values-body');
    const liveSec  = document.getElementById('live-values-section');
    const errEl    = document.getElementById('device-error-body');
    const errSec   = document.getElementById('device-error-section');
    if (!liveEl || !liveSec || !errEl || !errSec) return;

    const dev = devices_map[id];
    const online = dev && (dev.status === 'online' || dev.status === true || dev.status === '0' || dev.status === 0);
    const isErrorCode = dev && !online && typeof dev.status === 'string' && /^\d+$/.test(dev.status);
    
    const errorMsg = deviceErrors[id];
    const hasError = errorMsg || isErrorCode;

    if (hasError) {
        errSec.classList.remove('hidden');
        liveSec.classList.add('hidden'); // Hide live values on error
        
        let text = errorMsg || `Error Code: ${dev.status}`;
        errEl.innerHTML = `
            <div class="text-red-300 text-xs leading-relaxed flex items-center gap-2">
                <i class="fa-solid fa-triangle-exclamation text-red-500"></i>
                ${text}
            </div>`;
        return;
    }

    errSec.classList.add('hidden');
    const vals = liveValues[id];
    if (!vals || !Object.keys(vals).length) {
        liveSec.classList.add('hidden');
        return;
    }
    liveSec.classList.remove('hidden');
    liveEl.innerHTML = Object.entries(vals).map(([dp, { value, ts }]) =>
        `<div class="flex justify-between items-center text-xs py-1.5 border-b border-slate-700/50 last:border-0 gap-2">
            <span class="text-slate-400 font-mono shrink-0">${dp}</span>
            <span class="text-emerald-400 font-semibold">${JSON.stringify(value)}</span>
            <span class="text-slate-600 font-mono text-[10px] shrink-0">${ts}</span>
         </div>`
    ).join('');
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

    const statusEl = document.getElementById('details-status');
    if (statusEl) statusEl.innerHTML = renderStatusCell(dev);

    document.getElementById('details-content').innerHTML = Object.entries(dev)
        .filter(([k, v]) => !HIDDEN_DETAIL_KEYS.has(k) && v !== null && v !== undefined && typeof v !== 'object')
        .map(([k, v]) => renderDetailRow(k, v))
        .join('');

    updateDetailsLiveValues(id);


    document.getElementById('btn-edit').onclick   = () => { closeDetails(); openEditDeviceModal(id); };
    document.getElementById('btn-delete').onclick = async () => {
        const confirmed = await showConfirm({
            title: 'Delete Device',
            message: `Are you sure you want to delete ${dev.name || id}? This will remove it from the bridge configuration.`,
            okText: 'Delete',
        });
        if (confirmed) {
            sendCommand('remove', { id });
            // Preemptive UI removal for better responsiveness
            if (devices_map[id]) {
                delete devices_map[id];
                updateSyncStateAndRender();
            }
            closeDetails();
        }
    };

    panel.classList.remove('translate-x-full');
    
    // Show backdrop
    const backdrop = document.getElementById('panel-backdrop');
    if (backdrop) {
        backdrop.classList.remove('hidden');
        setTimeout(() => backdrop.classList.remove('opacity-0'), 10);
    }

    // Close log panel if open
    document.getElementById('log-panel')?.classList.add('translate-x-full');
}

function closeDetails() {
    const panel = document.getElementById('details-panel');
    if (panel) panel.classList.add('translate-x-full');
    currentDeviceId = null;
    hidePanelBackdrop();
}

function closeAllSidePanels() {
    closeDetails();
    closeLogPanel();
}

function hidePanelBackdrop() {
    const backdrop = document.getElementById('panel-backdrop');
    if (backdrop) {
        backdrop.classList.add('opacity-0');
        setTimeout(() => backdrop.classList.add('hidden'), 300);
    }
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
    if (!code) { showToast('Please enter a User Code.', 'warning'); return; }
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
        showToast(`Wizard failed: ${status.error}`, 'error', 6000);
        addLog(`Wizard failed: ${status.error}`, 'error');
        return;
    }

    document.getElementById('wizard-status-title').innerText = status.step;

    const hasQr     = !!status.url;
    const qrEl      = document.getElementById('wizard-qr-container');
    const spinnerEl = document.getElementById('wizard-spinner');

    qrEl.classList.toggle('hidden', !hasQr);
    qrEl.classList.toggle('flex',    hasQr);
    spinnerEl.classList.toggle('hidden', hasQr);

    if (hasQr) {
        try {
            // Generate QR code locally
            const typeNumber = 0; // auto
            const errorCorrectionLevel = 'L';
            const qr = qrcode(typeNumber, errorCorrectionLevel);
            qr.addData(status.url);
            qr.make();
            document.getElementById('wizard-qr-img').src = qr.createDataURL(6); // scale=6 for decent size
        } catch (err) {
            console.error('QR Gen Error:', err);
            showToast('Local QR generation failed.', 'error');
        }
        return;
    }

    if (!status.running) {
        spinnerEl.classList.add('hidden');
        showToast('Wizard complete! Cloud devices refreshed.', 'success', 4000);
        addLog('Wizard complete — cloud devices refreshed', 'success');
        setTimeout(() => { closeModal(); requestStatusUpdate(); }, 1500);
    }
}

// =============================================================================
// Sync modal
// =============================================================================
function requestSyncCheck() { requestStatusUpdate(); }

// =============================================================================
// Keyboard shortcuts
// =============================================================================
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        closeModal();
        closeDetails();
        closeLogPanel();
    }
});

// =============================================================================
// Bootstrap
// =============================================================================
connectWS();
