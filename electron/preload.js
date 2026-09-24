// Preload script running in an isolated context before the web page loads.
// Runs with contextIsolation:true and sandbox:true, so this file has no Node
// privileges beyond the contextBridge and the renderer can never reach
// `require`, `ipcRenderer`, the filesystem or child_process.
//
// The bridge below is the complete surface exposed to the page: a single
// read-only flag plus one event subscription used by the native menu item
// "Refresh / Sync Games" (frontend/app.js). Arbitrary window control
// (minimize/maximize/close) was removed: nothing in the UI called it and it
// let any script running in the page drive the app window.
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('desktopApp', {
    isDesktop: true,
    onSyncGames: (callback) => {
        if (typeof callback !== 'function') return () => {};
        const listener = () => {
            try {
                callback();
            } catch (err) {
                console.error('[desktopApp] onSyncGames handler failed:', err);
            }
        };
        ipcRenderer.on('menu-sync-games', listener);
        return () => ipcRenderer.removeListener('menu-sync-games', listener);
    }
});
