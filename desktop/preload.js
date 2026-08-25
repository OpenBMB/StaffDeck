/**
 * StaffDeck 桌面壳 —— 渲染进程预加载脚本（安全桥）
 *
 * 保持最小桥接面：渲染层默认不需要访问 Node。若后续需要给前端暴露
 * 平台信息等，再在此处用 contextBridge 增量暴露，避免开启 nodeIntegration。
 */
const { contextBridge } = require('electron');

contextBridge.exposeInMainWorld('staffdeckDesktop', {
  platform: process.platform,
  version: process.env.npm_package_version || '',
});
