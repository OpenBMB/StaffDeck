# Extend a running agent with Cordis tools

English | [中文](dynamic-cordis.zh.md)

This practice guide enables [`@deepseek-ai/dsh-tool-cordis`](../../../../packages/extensions/tool-cordis/README.md). The agent can inspect its current Cordis process and mount or unmount model-authored plugins in memory. Temporary plugins disappear when they are unmounted or the process exits and may affect other sessions in the same process.

## Run it

Start the browser interface with the checked-in overlay:

```sh
pnpm dsh web --patch apps/cli/config/examples/cordis/cordis.yml
```

The command requires a model credential. The [Cordis tool reference](../../../../packages/extensions/tool-cordis/README.md) defines the tool arguments, lifetime, cleanup, and safety contracts.
