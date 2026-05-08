import { useEffect } from 'react';
import useStore from './store/useStore';
import { Activity, Radio, Terminal as TerminalIcon } from 'lucide-react';
import './index.css';

const DeviceCard = ({ device, sendCommand }) => {
  const isOnline = device.status === 'online';
  
  return (
    <div className="device-card">
      <div className="device-header">
        <div>
          <h3 className="device-title">{device.name || 'Unknown Device'}</h3>
          <div className="device-id">{device.id}</div>
        </div>
        <div className={`device-status ${isOnline ? 'online' : 'offline'}`}>
          {device.status || 'offline'}
        </div>
      </div>
      
      <div className="dp-list">
        {Object.entries(device.dps || {}).map(([key, value]) => (
          <div key={key} className="dp-item">
            <span className="dp-key">DP {key}</span>
            <span className="dp-value">{JSON.stringify(value)}</span>
          </div>
        ))}
        {Object.keys(device.dps || {}).length === 0 && (
          <div className="dp-item" style={{justifyContent: 'center', color: 'var(--text-secondary)'}}>
            No data points yet
          </div>
        )}
      </div>

      <div className="controls">
        <button className="btn" onClick={() => sendCommand('status', { id: device.id })}>
          Refresh Status
        </button>
        <button className="btn btn-danger" onClick={() => sendCommand('remove', { id: device.id })}>
          Remove
        </button>
      </div>
    </div>
  );
};

const Terminal = () => {
  const logs = useStore((state) => state.logs);
  
  return (
    <div className="terminal-panel">
      <div className="terminal-header">
        <div style={{display: 'flex', alignItems: 'center', gap: '0.5rem'}}>
          <TerminalIcon size={16} /> MQTT Monitor
        </div>
      </div>
      <div className="terminal-logs">
        {logs.map((log, i) => (
          <div key={i} className="log-entry">
            <span className="log-time">[{log.time}]</span>
            <span className="log-topic">{log.topic}</span>
            <span className="log-payload">{JSON.stringify(log.payload)}</span>
          </div>
        ))}
        {logs.length === 0 && <div style={{color: 'var(--text-secondary)'}}>Waiting for messages...</div>}
      </div>
    </div>
  );
};

const WizardModal = () => {
  const wizardState = useStore((state) => state.wizardState);
  if (!wizardState.running) return null;

  return (
    <div style={{
      position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
      background: 'rgba(0,0,0,0.8)', backdropFilter: 'blur(4px)',
      display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000
    }}>
      <div style={{
        background: 'var(--panel-bg)', padding: '2rem', borderRadius: '16px',
        border: '1px solid var(--panel-border)', maxWidth: '500px', width: '100%',
        textAlign: 'center'
      }}>
        <h2>Tuya Setup Wizard</h2>
        <p style={{color: 'var(--text-secondary)'}}>{wizardState.step}</p>
        
        {wizardState.url && (
          <div style={{margin: '1.5rem 0'}}>
            <a href={wizardState.url} target="_blank" rel="noreferrer" 
               style={{color: 'var(--accent)', textDecoration: 'none'}}>
               Click here or scan QR in terminal
            </a>
          </div>
        )}
        
        {wizardState.error && (
          <div style={{color: 'var(--danger)', marginTop: '1rem', padding: '1rem', background: 'rgba(239, 68, 68, 0.1)', borderRadius: '8px'}}>
            {wizardState.error}
          </div>
        )}
        
        <div style={{marginTop: '2rem'}}>
          <Activity size={32} className="spinner" style={{animation: 'spin 2s linear infinite', opacity: 0.5}} />
          <style>{`@keyframes spin { 100% { transform: rotate(360deg); } }`}</style>
        </div>
      </div>
    </div>
  );
};

function App() {
  const { devices, mqttConnected, setSocket, setMqttConnected, setDevices, addLog, updateDeviceStatus, sendCommand, setWizardState } = useStore();

  useEffect(() => {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = process.env.NODE_ENV === 'development' 
      ? 'ws://localhost:8373/ws' 
      : `${protocol}//${window.location.host}/ws`;
      
    const ws = new WebSocket(wsUrl);

    ws.onopen = () => {
      setSocket(ws);
      // Automatically request full status from the bridge upon connection
      ws.send(JSON.stringify({ action: 'mqtt_publish', topic: 'rustuya/command', payload: { action: 'status' } }));
    };

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        
        if (msg.type === 'init') {
          setMqttConnected(msg.mqtt_connected);
          if (msg.cloud_devices) setDevices(msg.cloud_devices);
        } else if (msg.type === 'wizard') {
          setWizardState(msg.status);
          if (!msg.status.running && msg.cloud_devices) {
            setDevices(msg.cloud_devices); // Update device list on finish
          }
        } else if (msg.type === 'mqtt_status') {
          setMqttConnected(msg.connected);
        } else if (msg.type === 'mqtt_message') {
          const time = new Date().toLocaleTimeString();
          addLog({ time, topic: msg.topic, payload: msg.payload });
          
          // Simple topic parsing for device state update
          // Expected topic: rustuya/event/status/{id} or similar
          const parts = msg.topic.split('/');
          const id = parts[parts.length - 1]; // Assume last part is ID for now
          
          if (id && msg.payload) {
            let status = 'online';
            let dps = {};
            
            if (msg.topic.includes('error') && msg.payload.errorCode !== 0) {
              status = 'error';
            }
            
            if (typeof msg.payload === 'object') {
              if (msg.payload.dps) dps = msg.payload.dps;
              else {
                // Ignore junk keys
                const junk = ['errorCode', 'errorMsg', 'payloadStr', 'id', 'action'];
                Object.keys(msg.payload).forEach(k => {
                  if (!junk.includes(k)) dps[k] = msg.payload[k];
                });
              }
            }
            updateDeviceStatus(id, status, dps);
          }
        }
      } catch (err) {
        console.error('Failed to parse WS message:', err);
      }
    };

    ws.onclose = () => {
      setSocket(null);
      setMqttConnected(false);
      // Optional: implement reconnect logic
    };

    return () => ws.close();
  }, [setSocket, setMqttConnected, setDevices, addLog, updateDeviceStatus]);

  return (
    <div className="app-container">
      <header className="header">
        <h1>Rustuya Manager</h1>
        <div style={{display: 'flex', gap: '1rem'}}>
          <div className="status-badge">
            <div className={`status-dot ${mqttConnected ? 'connected' : 'disconnected'}`}></div>
            MQTT {mqttConnected ? 'Connected' : 'Disconnected'}
          </div>
          <button className="btn" onClick={() => sendCommand('wizard_start', {})}>
            <Activity size={16} /> Run Wizard
          </button>
        </div>
      </header>

      <main className="main-content">
        <div className="devices-grid">
          {Object.values(devices).map(device => (
            <DeviceCard key={device.id} device={device} sendCommand={sendCommand} />
          ))}
          {Object.keys(devices).length === 0 && (
            <div style={{color: 'var(--text-secondary)', gridColumn: '1 / -1', textAlign: 'center', padding: '3rem'}}>
              <Radio size={48} style={{opacity: 0.5, marginBottom: '1rem'}} />
              <p>No devices found. Run the wizard or check MQTT connection.</p>
            </div>
          )}
        </div>
        
        <Terminal />
      </main>
      <WizardModal />
    </div>
  );
}

export default App;
