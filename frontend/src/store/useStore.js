import { create } from 'zustand';

const useStore = create((set, get) => ({
  devices: {},
  mqttConnected: false,
  logs: [],
  wizardState: { running: false, step: '', url: null, error: null },
  socket: null,

  setSocket: (socket) => set({ socket }),
  
  updateDeviceStatus: (id, status, dps = {}) => set((state) => {
    const existing = state.devices[id] || { id, name: `Unknown (${id})`, dps: {} };
    return {
      devices: {
        ...state.devices,
        [id]: {
          ...existing,
          status,
          dps: { ...existing.dps, ...dps }
        }
      }
    };
  }),

  setDevices: (devices) => set({ devices }),
  
  setMqttConnected: (connected) => set({ mqttConnected: connected }),

  addLog: (log) => set((state) => ({
    logs: [log, ...state.logs].slice(0, 100) // Keep last 100 logs
  })),

  setWizardState: (wizardState) => set({ wizardState }),

  sendCommand: (action, payload) => {
    const { socket } = get();
    if (socket && socket.readyState === WebSocket.OPEN) {
      if (action === 'mqtt_publish') {
        socket.send(JSON.stringify({ action: 'mqtt_publish', ...payload }));
      } else {
        socket.send(JSON.stringify({ action, payload }));
      }
    }
  }
}));

export default useStore;
