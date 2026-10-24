const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("markdownApi", {
  consume: () => ipcRenderer.invoke("lantai:markdown-consume"),
  chooseOpen: () => ipcRenderer.invoke("lantai:markdown-choose-open"),
  save: (payload) => ipcRenderer.invoke("lantai:markdown-save", payload || {}),
  handoffToChat: (payload) =>
    ipcRenderer.invoke("lantai:markdown-handoff-to-chat", payload || {}),
  setDirty: (dirty) => ipcRenderer.send("lantai:markdown-dirty", dirty === true),
  reportCloseSaved: (saved) => ipcRenderer.send("lantai:markdown-close-saved", saved === true),
  onCommand: (callback) => {
    const listener = (_event, command) => callback(String(command || ""));
    ipcRenderer.on("lantai:markdown-command", listener);
    return () => ipcRenderer.removeListener("lantai:markdown-command", listener);
  },
});
