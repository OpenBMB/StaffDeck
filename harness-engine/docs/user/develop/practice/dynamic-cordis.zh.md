# 用 Cordis 工具扩展运行中的智能体

[English](dynamic-cordis.md) | 中文

本实战指南启用 [`@deepseek-ai/dsh-tool-cordis`](../../../../packages/extensions/tool-cordis/README.zh.md)。智能体可以检查当前 Cordis 进程，并在内存中挂载或卸载模型编写的插件。临时插件会在卸载或进程退出时消失，并可能影响同一进程中的其他会话。

## 运行

使用仓库内 overlay 启动浏览器界面：

```sh
pnpm dsh web --patch apps/cli/config/examples/cordis/cordis.yml
```

该命令需要模型凭据。[Cordis 工具参考](../../../../packages/extensions/tool-cordis/README.zh.md)定义了四类约定：工具参数、存续时间、清理行为和安全性。
