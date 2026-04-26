let ws = null;
let reconnectTimer = null;
let devices_map = {};

// --- Navigation & Layout ---
function showSection(sectionId) {
    document.querySelectorAll('.section').forEach(s => s.classList.add('hidden'));
    document.getElementById(sectionId).classList.remove('hidden');
    
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    event.currentTarget.classList.add('active');
    
    const titles = {
        'dashboard': 'Device Dashboard',
        'devices': 'Topology View',
        'settings': 'Settings'
    };
    document.getElementById('page-title').innerText = titles[sectionId] || 'Dashboard';
}

// --- WebSocket Connection ---
function connectWS() {
    ws = new WebSocket(`ws://${window.location.host}/ws`);
    
    ws.onopen = () => {
        clearInterval(reconnectTimer);
        updateConnectionStatus(true);
        // Ensure UI pulls state right after connection is established
        requestStatusUpdate();
    };
    
    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        if (data.type === 'wizard') {
            handleWizardEvent(data.status);
            return;
        }
        if (data.type === 'init' || data.devices_updated || data.type === 'status') {
            if (data.cloud_devices) cloud_devices = data.cloud_devices;
            if (data.devices) devices_map = data.devices;
            if (data.type === 'init' && data.user_code) {
                const codeEl = document.getElementById('wizard-code');
                if (codeEl) codeEl.value = data.user_code;
            }
            updateSyncStateAndRender();
        }
    };
    
    ws.onclose = () => {
        updateConnectionStatus(false);
        // Retry connection every 5s
        reconnectTimer = setTimeout(connectWS, 5000);
    };
    
    ws.onerror = (err) => {
        console.error('WS Error', err);
        ws.close();
    };
}

function updateSyncStateAndRender() {
    currentSyncData = { missing: [], mismatched: [], orphaned: [], synced: [] };
    
    const hasCloud = Object.keys(cloud_devices).length > 0;
    
    if (hasCloud) {
        const bridge_ids = Object.keys(devices_map);
        const cloud_ids = Object.keys(cloud_devices);
        
        for (const cid of cloud_ids) {
            const cdev = cloud_devices[cid];
            if (!bridge_ids.includes(cid)) {
                currentSyncData.missing.push(cdev);
            } else {
                const bdev = devices_map[cid];
                const diff = [];
                if (cdev.name && bdev.name && cdev.name !== bdev.name) diff.push("name");
                const ckey = cdev.key || cdev.local_key || cdev.localkey;
                const bkey = bdev.key || bdev.local_key;
                if (ckey && bkey && ckey !== bkey) diff.push("local_key");
                
                if (diff.length > 0) {
                    currentSyncData.mismatched.push({cloud: cdev, bridge: bdev, reasons: diff});
                } else {
                    currentSyncData.synced.push(bdev);
                }
            }
        }
        for (const bid of bridge_ids) {
            if (!cloud_ids.includes(bid)) {
                currentSyncData.orphaned.push(devices_map[bid]);
            }
        }
    } else {
        currentSyncData.synced = Object.values(devices_map);
    }
    
    updateStatsCards();
    renderSyncPanels();
    renderDashboard();
}

function updateStatsCards() {
    document.getElementById('stat-total').innerText = Object.keys(devices_map).length;
    document.getElementById('stat-cloud').innerText = Object.keys(cloud_devices).length;
    document.getElementById('stat-synced').innerText = currentSyncData.synced.length;
    document.getElementById('stat-conflicts').innerText = currentSyncData.missing.length + currentSyncData.mismatched.length;
}

function renderSyncPanels() {
    // Missing
    document.getElementById('count-missing').innerText = currentSyncData.missing.length;
    document.getElementById('section-missing').classList.toggle('hidden', currentSyncData.missing.length === 0);
    const mBody = document.getElementById('body-missing');
    mBody.innerHTML = '';
    currentSyncData.missing.forEach(dev => {
        mBody.innerHTML += `<div class="flex justify-between items-center text-sm border-b border-slate-700/50 pb-2 mb-2 last:border-0"><span class="text-white">${dev.name} <span class="text-slate-500 font-mono text-xs">(${dev.id})</span></span><button onclick="resolveSingleMissing('${dev.id}', event)" class="text-rose-400 hover:text-white px-3 py-1.5 rounded bg-rose-500/10 hover:bg-rose-500/30 transition-colors border border-rose-500/20">Import to Bridge</button></div>`;
    });
    
    // Mismatched
    document.getElementById('count-mismatch').innerText = currentSyncData.mismatched.length;
    document.getElementById('section-mismatch').classList.toggle('hidden', currentSyncData.mismatched.length === 0);
    const mmBody = document.getElementById('body-mismatch');
    mmBody.innerHTML = '';
    currentSyncData.mismatched.forEach(item => {
        const dev = item.cloud;
        mmBody.innerHTML += `<div class="flex flex-col text-sm border-b border-slate-700/50 pb-2 mb-2 last:border-0">
            <div class="flex justify-between items-center"><span class="text-white">${dev.name} <span class="text-slate-500 font-mono text-xs">(${dev.id})</span></span><button onclick="resolveSingleMismatch('${dev.id}', event)" class="text-amber-400 hover:text-white px-3 py-1.5 rounded bg-amber-500/10 hover:bg-amber-500/30 transition-colors border border-amber-500/20">Push Config to Bridge</button></div>
            <div class="text-xs text-slate-400 mt-1"><span class="text-amber-400">Conflicts detected:</span> ${item.reasons.join(', ')}</div>
        </div>`;
    });
    
    // Orphaned
    document.getElementById('count-orphan').innerText = currentSyncData.orphaned.length;
    document.getElementById('section-orphan').classList.toggle('hidden', currentSyncData.orphaned.length === 0);
    const oBody = document.getElementById('body-orphan');
    oBody.innerHTML = '';
    currentSyncData.orphaned.forEach(dev => {
        oBody.innerHTML += `<div class="flex justify-between items-center text-sm border-b border-slate-700/50 pb-2 mb-2 last:border-0"><span class="text-white">${dev.name} <span class="text-slate-500 font-mono text-xs">(${dev.id})</span></span><button onclick="resolveSingleOrphan('${dev.id}', event)" class="text-slate-400 hover:text-white px-3 py-1.5 rounded bg-slate-700 hover:bg-red-500/20 hover:text-red-400 transition-colors border border-slate-600 hover:border-red-500/30">Delete from Bridge</button></div>`;
    });
    
    // Synced
    document.getElementById('count-synced').innerText = currentSyncData.synced.length;
}

function resolveMissing(e) {
    if(e) e.stopPropagation();
    currentSyncData.missing.forEach(dev => submitDeviceBridgeAdd(dev));
}

function resolveSingleMissing(id, e) {
    if(e) e.stopPropagation();
    const dev = currentSyncData.missing.find(d => d.id === id);
    if(dev) submitDeviceBridgeAdd(dev);
}

function resolveMismatched(e) {
    if(e) e.stopPropagation();
    currentSyncData.mismatched.forEach(item => submitDeviceBridgeAdd(item.cloud));
}

function resolveSingleMismatch(id, e) {
    if(e) e.stopPropagation();
    const item = currentSyncData.mismatched.find(d => d.cloud.id === id);
    if(item) submitDeviceBridgeAdd(item.cloud);
}

function resolveOrphans(e) {
    if(e) e.stopPropagation();
    currentSyncData.orphaned.forEach(dev => sendCommand('remove', { id: dev.id }));
}

function resolveSingleOrphan(id, e) {
    if(e) e.stopPropagation();
    sendCommand('remove', { id: id });
}

function isPrivateIP(ip) {
    if (!ip || typeof ip !== 'string') return false;
    if (ip.toLowerCase() === 'auto') return true; // Keep 'Auto' if explicitly set
    const parts = ip.split('.');
    if (parts.length !== 4) return false;
    if (parts[0] === '10') return true;
    if (parts[0] === '192' && parts[1] === '168') return true;
    if (parts[0] === '172') {
        const p2 = parseInt(parts[1], 10);
        if (p2 >= 16 && p2 <= 31) return true;
    }
    return false;
}

function submitDeviceBridgeAdd(dev) {
    const payload = {
        id: dev.id,
        name: dev.name || 'Unnamed',
    };
    if (dev.sub || dev.parent || dev.parent_id) {
        payload.node_id = dev.node_id || dev.cid;
        payload.parent = dev.parent || dev.parent_id;
    } else {
        payload.key = dev.key || dev.local_key || dev.localkey;
        let ip = dev.ip || 'Auto';
        if (!isPrivateIP(ip)) {
            ip = 'Auto';
        }
        payload.ip = ip;
        payload.version = dev.version || '3.3';
    }
    sendCommand('add', payload);
}

function openWizardModal() {
    document.getElementById('device-modal').classList.add('hidden');
    const wizardModal = document.getElementById('wizard-modal');
    wizardModal.classList.remove('hidden');
    
    document.getElementById('wizard-input-step').classList.remove('hidden');
    document.getElementById('wizard-loading-step').classList.add('hidden');
    document.getElementById('wizard-qr-container').classList.add('hidden');
    document.getElementById('wizard-spinner').classList.add('hidden');
    
    const overlay = document.getElementById('modal-overlay');
    overlay.classList.remove('opacity-0', 'pointer-events-none');
    wizardModal.classList.remove('scale-95');
}

function startWizard() {
    const code = document.getElementById('wizard-code').value;
    if (!code) {
        alert("Please enter a User Code.");
        return;
    }
    
    document.getElementById('wizard-input-step').classList.add('hidden');
    document.getElementById('wizard-loading-step').classList.remove('hidden');
    document.getElementById('wizard-spinner').classList.remove('hidden');
    document.getElementById('wizard-status-title').innerText = "Starting API Login...";
    document.getElementById('wizard-status-msg').innerText = "Please wait.";
    
    sendCommand('wizard_start', { user_code: code });
}

function handleWizardEvent(status) {
    if (status.error) {
        document.getElementById('wizard-status-title').innerHTML = "<span class='text-red-500'>Error Complete</span>";
        document.getElementById('wizard-status-msg').innerText = status.error;
        document.getElementById('wizard-spinner').classList.add('hidden');
        document.getElementById('wizard-qr-container').classList.add('hidden');
        return;
    }
    
    document.getElementById('wizard-status-title').innerText = status.step;
    
    if (status.url) {
        document.getElementById('wizard-qr-container').classList.remove('hidden');
        document.getElementById('wizard-qr-container').classList.add('flex');
        document.getElementById('wizard-qr-img').src = `https://api.qrserver.com/v1/create-qr-code/?size=250x250&data=${encodeURIComponent(status.url)}`;
        document.getElementById('wizard-spinner').classList.add('hidden');
    } else {
        document.getElementById('wizard-qr-container').classList.add('hidden');
        document.getElementById('wizard-qr-container').classList.remove('flex');
        if (status.running) {
             document.getElementById('wizard-spinner').classList.remove('hidden');
        } else {
             document.getElementById('wizard-spinner').classList.add('hidden');
             setTimeout(() => {
                 closeModal();
                 requestStatusUpdate();
             }, 1500);
        }
    }
}

function updateConnectionStatus(connected) {
    const el = document.getElementById('connection-status');
    if (connected) {
        el.innerHTML = `
            <span class="status-dot status-online"></span>
            <span class="text-slate-300 font-medium tracking-wide">Connected</span>
        `;
    } else {
        el.innerHTML = `
            <span class="relative flex h-3 w-3 mr-3">
                <span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-red-400 opacity-75"></span>
                <span class="relative inline-flex rounded-full h-3 w-3 bg-red-500"></span>
            </span>
            <span class="text-slate-300 font-medium tracking-wide">Disconnected</span>
        `;
    }
}

function sendCommand(action, payload = {}) {
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ action, payload }));
    } else {
        alert("Cannot send command, disconnected from backend");
    }
}

function requestStatusUpdate() {
    sendCommand('status');
    const icon = document.querySelector('.fa-rotate-right');
    icon.classList.add('fa-spin');
    setTimeout(() => icon.classList.remove('fa-spin'), 1000);
}

// --- Rendering ---
function renderDashboard() {
    const tbody = document.getElementById('devices-body');
    const search = document.getElementById('search-input').value.toLowerCase();
    
    tbody.innerHTML = '';
    
    const devices = currentSyncData.synced || Object.values(devices_map);
    if (devices.length === 0) {
        tbody.innerHTML = `<tr><td colspan="5" class="py-12 text-center text-slate-500">No strictly synced devices found.</td></tr>`;
        return;
    }
    
    const rootNodes = [];
    const childMap = {};
    
    devices.forEach(d => {
        const parentId = d.parent || d.parent_id;
        if (parentId) {
            if (!childMap[parentId]) childMap[parentId] = [];
            childMap[parentId].push(d);
        } else {
            rootNodes.push(d);
        }
    });

    function appendDeviceRow(dev, indentLevel) {
        const showDevice = !search || ((dev.name || '').toLowerCase().includes(search) || (dev.id || '').toLowerCase().includes(search));
        
        if (!search || showDevice) {
            const isOnline = dev.status === undefined || dev.status === 'online' || dev.status === true;
            const typeStr = dev.sub || (dev.parent || dev.parent_id) ? 'Zigbee/BLE' : 'WiFi';
            const iconType = typeStr === 'WiFi' ? 'fa-wifi' : 'fa-network-wired';
            
            const tr = document.createElement('tr');
            tr.onclick = () => openDetails(dev.id);
            tr.className = "border-b border-slate-700/50 hover:bg-slate-800/40 transition-colors cursor-pointer group";
            
            const indentPadding = search ? 0 : (indentLevel * 2);
            const indentIcon = indentLevel > 0 && (!search) ? `<i class="fa-solid fa-level-up-alt fa-rotate-90 text-slate-600 mr-2 opacity-70"></i>` : '';

            tr.innerHTML = `
                <td class="py-4 px-5">
                    <div class="flex items-center" style="padding-left: ${indentPadding}rem">
                        ${indentIcon}
                        <span class="status-dot ${isOnline ? 'status-online' : 'status-offline'}"></span>
                        <span class="text-sm font-medium ${isOnline ? 'text-slate-300' : 'text-slate-500'}">
                            ${isOnline ? 'Online' : (dev.status === false ? 'Offline' : (dev.status || 'Offline'))}
                        </span>
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
                    <button class="text-slate-500 hover:text-white p-2 rounded hover:bg-slate-700 transition-colors" onclick="event.stopPropagation(); openDetails('${dev.id}')">
                        <i class="fa-solid fa-chevron-right"></i>
                    </button>
                </td>
            `;
            tbody.appendChild(tr);
        }

        // Render children
        if (!search && childMap[dev.id]) {
            childMap[dev.id].forEach(child => appendDeviceRow(child, indentLevel + 1));
        }
    }

    if (search) {
        devices.forEach(d => appendDeviceRow(d, 0));
    } else {
        if (rootNodes.length === 0 && devices.length > 0) {
             devices.forEach(d => appendDeviceRow(d, 0));
        } else {
             rootNodes.forEach(d => appendDeviceRow(d, 0));
        }
    }
    
    populateParentsSelect();
}

function updateStats() {
    const devices = Object.values(devices_map);
    document.getElementById('stat-total').innerText = devices.length;
    document.getElementById('stat-online').innerText = devices.filter(d => d.status === undefined || d.status === 'online' || d.status === true).length;
    document.getElementById('stat-offline').innerText = devices.filter(d => d.status === 'offline' || d.status === false).length;
    document.getElementById('stat-sub').innerText = devices.filter(d => d.sub || (d.parent || d.parent_id)).length;
}

function renderTree() {
    const container = document.getElementById('tree-container');
    container.innerHTML = '';
    
    // Build tree
    const rootNodes = [];
    const childMap = {};
    
    const devices = Object.values(devices_map);
    devices.forEach(d => {
        const parentId = d.parent || d.parent_id;
        if (parentId) {
            if (!childMap[parentId]) childMap[parentId] = [];
            childMap[parentId].push(d);
        } else {
            rootNodes.push(d);
        }
    });
    
    function createNodeElement(dev, isRoot) {
        const div = document.createElement('div');
        const iconType = dev.parent ? 'fa-microchip' : 'fa-network-wired';
        const color = dev.status === 'online' ? 'text-green-400' : 'text-slate-500';
        
        div.innerHTML = `
            <div class="tree-node" onclick="openDetails('${dev.id}')">
                <i class="fa-solid ${iconType} ${color} w-6 text-center mr-2"></i>
                <div>
                    <div class="text-sm font-medium text-white">${dev.name || 'Unnamed'}</div>
                    <div class="text-xs text-slate-500 font-mono">${dev.id}</div>
                </div>
            </div>
        `;
        
        const children = childMap[dev.id] || [];
        if (children.length > 0) {
            const childrenContainer = document.createElement('div');
            childrenContainer.className = 'tree-children';
            children.forEach(c => childrenContainer.appendChild(createNodeElement(c, false)));
            div.appendChild(childrenContainer);
        }
        
        return div;
    }
    
    if (rootNodes.length === 0 && devices.length > 0) {
         // Fallback if no hierarchical root
         devices.forEach(d => container.appendChild(createNodeElement(d, true)));
    } else {
         rootNodes.forEach(d => container.appendChild(createNodeElement(d, true)));
    }
}

function filterTable() {
    renderDashboard();
}

function populateParentsSelect() {
    const select = document.getElementById('dev-parent');
    const prevVal = select.value;
    select.innerHTML = '<option value="">Select Parent...</option>';
    Object.values(devices_map).forEach(dev => {
        if (!dev.parent && !dev.sub) {
            const opt = document.createElement('option');
            opt.value = dev.id;
            opt.text = `${dev.name || 'Unnamed'} (${dev.id})`;
            select.appendChild(opt);
        }
    });
    // try to restore
    if(Array.from(select.options).some(o => o.value === prevVal)) {
        select.value = prevVal;
    }
}

// --- Modals & Panels ---
let currentDeviceId = null;

function openAddDeviceModal() {
    currentDeviceId = null;
    document.getElementById('modal-title').innerText = "Add Device";
    document.getElementById('device-form').reset();
    document.getElementById('dev-id').readOnly = false;
    toggleDeviceFields();
    
    const overlay = document.getElementById('modal-overlay');
    const modal = document.getElementById('device-modal');
    modal.classList.remove('hidden');
    overlay.classList.remove('opacity-0', 'pointer-events-none');
    modal.classList.remove('scale-95');
}

function openEditDeviceModal(id) {
    const dev = devices_map[id];
    if (!dev) return;
    
    currentDeviceId = id;
    document.getElementById('modal-title').innerText = "Edit Device";
    
    document.getElementById('dev-id').value = id;
    document.getElementById('dev-id').readOnly = true; // Disable modifying ID
    document.getElementById('dev-name').value = dev.name || '';
    
    const isZigbee = !!(dev.parent || dev.sub || dev.node_id);
    document.querySelector(`input[name="dev-type"][value="${isZigbee ? 'Zigbee/BLE' : 'WiFi'}"]`).checked = true;
    toggleDeviceFields();
    
    if (isZigbee) {
        document.getElementById('dev-node').value = dev.node_id || dev.cid || '';
        document.getElementById('dev-parent').value = dev.parent || '';
    } else {
        document.getElementById('dev-key').value = dev.key || dev.local_key || '';
        document.getElementById('dev-ip').value = dev.ip || '';
        document.getElementById('dev-version').value = dev.version || '';
    }
    
    const overlay = document.getElementById('modal-overlay');
    const modal = document.getElementById('device-modal');
    modal.classList.remove('hidden');
    overlay.classList.remove('opacity-0', 'pointer-events-none');
    modal.classList.remove('scale-95');
}

function closeModal() {
    const overlay = document.getElementById('modal-overlay');
    const deviceModal = document.getElementById('device-modal');
    const wizardModal = document.getElementById('wizard-modal');
    const syncModal = document.getElementById('sync-modal');
    
    overlay.classList.add('opacity-0', 'pointer-events-none');
    deviceModal.classList.add('scale-95');
    wizardModal.classList.add('scale-95');
    syncModal.classList.add('scale-95');
    
    setTimeout(() => {
        deviceModal.classList.add('hidden');
        wizardModal.classList.add('hidden');
        syncModal.classList.add('hidden');
    }, 300);
}

function toggleDeviceFields() {
    const type = document.querySelector('input[name="dev-type"]:checked').value;
    if (type === 'WiFi') {
        document.getElementById('fields-wifi').classList.remove('hidden');
        document.getElementById('fields-zigbee').classList.add('hidden');
    } else {
        document.getElementById('fields-wifi').classList.add('hidden');
        document.getElementById('fields-zigbee').classList.remove('hidden');
    }
}

function submitDeviceForm(e) {
    e.preventDefault();
    const payload = {
        id: document.getElementById('dev-id').value,
        name: document.getElementById('dev-name').value,
    };
    
    const type = document.querySelector('input[name="dev-type"]:checked').value;
    if (type === 'WiFi') {
        payload.key = document.getElementById('dev-key').value;
        payload.ip = document.getElementById('dev-ip').value || "Auto";
        payload.version = document.getElementById('dev-version').value || "Auto";
    } else {
        payload.cid = document.getElementById('dev-node').value;
        payload.parent_id = document.getElementById('dev-parent').value;
    }
    
    // In old logic, if editing, we might need to delete then add or just add to override
    sendCommand('add', payload);
    closeModal();
    
    // Optimistically hide details to refresh
    closeDetails();
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
    
    const content = document.getElementById('details-content');
    content.innerHTML = '';
    
    Object.entries(dev).forEach(([key, val]) => {
        if (typeof val === 'object' || !val) return;
        content.innerHTML += `
            <div class="detail-item">
                <span class="detail-label">${key.toUpperCase()}</span>
                <span class="detail-value" title="${val}">${val}</span>
            </div>
        `;
    });
    
    document.getElementById('btn-edit').onclick = () => {
        closeDetails();
        openEditDeviceModal(id);
    };
    
    document.getElementById('btn-delete').onclick = () => {
        if (confirm(`Are you sure you want to delete ${dev.name || id}?`)) {
            sendCommand('delete', { id });
            closeDetails();
        }
    };
    
    document.getElementById('details-panel').classList.remove('translate-x-full');
}

function closeDetails() {
    document.getElementById('details-panel').classList.add('translate-x-full');
}

// Initial
connectWS();
