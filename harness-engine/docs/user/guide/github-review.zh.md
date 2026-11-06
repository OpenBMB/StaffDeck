# 通过 GitHub Webhook 创建评审会话

[English](github-review.md) | 中文

此可选 overlay 会为 `dsh web` 增加一个签名 GitHub 端点。当已配置仓库中的 pull request 从 draft 变为 ready for review 时，规则会在该仓库的 Web Workspace 下创建带标题的根 Session，并启动只读评审提示词。

## 前置条件

- 一个可由 DSH 注册为 Web Workspace 的本地 checkout。
- 一个可通过 `DSH_GITHUB_WEBHOOK_SECRET` 凭据引用访问的高熵 GitHub webhook 密钥。
- 一个可以把单个公共 URL 转发到 loopback 监听器的 TLS 反向代理或 tunnel。
- GitHub webhook 订阅 Pull requests 事件，且 content type 为 `application/json`。

overlay 默认使用启动目录作为 Workspace，并监听 `127.0.0.1:3081`。可通过 `DSH_GITHUB_REVIEW_WORKSPACE` 与 `DSH_GITHUB_WEBHOOK_PORT` 覆盖它们。

## 启动 DSH

生成密钥，并在重启后继续使用同一值：

```sh
export DSH_GITHUB_WEBHOOK_SECRET="$(openssl rand -hex 32)"
printf '%s\n' "$DSH_GITHUB_WEBHOOK_SECRET"
```

在开发 checkout 中运行：

```sh
export DSH_GITHUB_REVIEW_WORKSPACE=/path/to/deepseek-harness
pnpm dsh web --patch apps/cli/config/examples/github-review/cordis.yml
```

安装版 DSH 通过绝对路径使用同一 overlay：

```sh
dsh web --patch /absolute/path/to/github-review/cordis.yml
```

对于永久 profile，把 `github-ready-review-rule.mjs` 放在 `$DSH_HOME/profiles/web/cordis.patch.yml` 旁边，把 `cordis.yml` 中的行追加到该 patch，然后运行 `dsh web`。随附 CLI 已经包含两个 webhook 包；只需 overlay 即可激活它们。

## 暴露专用端点

主 Web UI 与 `/api` 继续位于端口 3080。overlay 会在隔离 realm 中挂载第二个 WebServer；其中只注册 `POST /github`，其他路径均返回 `404`。

Caddy 配置可以只暴露该监听器：

```caddyfile
hooks.example.com {
  route {
    @github path /github
    reverse_proxy @github 127.0.0.1:3081
    respond 404
  }
}
```

GitHub 配置如下：

```text
Payload URL:  https://hooks.example.com/github
Content type: application/json
Secret:       DSH_GITHUB_WEBHOOK_SECRET value
Events:       Pull requests
Active:       yes
```

## 规则行为

规则只接受来源 `primary-github`、仓库 `deepseek-harness/deepseek-harness`、事件 `pull_request` 与动作 `ready_for_review`。它会把精确 head SHA 和选定 PR 字段传给评审提示词，把 JSON 标为不受信任的元数据，并禁止修改文件、分支、PR 或 GitHub 状态。

Session 请求选择 `standard` agent preset 与 `read-only` permission preset。`workspacePath` 通过 `WorkspaceRegistry.create()` 规范化，因此第一次匹配交付会在 Workspace 不存在时创建它，后续交付会复用它。

HTTP 响应刻意弱于 Agent 结果：`202` 表示签名与 JSON 已被接受，规则调用已在内存中调度。它不表示此规则已经匹配，也不表示已创建 Session。

## 程序化扩展

`run()` 是普通受信任 JavaScript。部署可以在返回 Session 请求前查询内部策略服务：

```js
const response = await fetch('https://policy.internal/pr-review', {
  method: 'POST',
  headers: { 'content-type': 'application/json' },
  body: JSON.stringify({ repository: payload.repository.full_name }),
  signal,
})
if (!response.ok || (await response.json()).automaticReview !== true) return null
```

它还可以把仓库映射到不同本地路径：

```js
const workspacePath = {
  'deepseek-harness/deepseek-harness': '/path/to/deepseek-harness',
  'deepseek-harness/dsh-sdk': '/path/to/dsh-sdk',
}[payload.repository.full_name]
if (workspacePath === undefined) return null
```

## 交付语义

webhook runtime 不存储交付或执行状态。重复交付会运行规则，并可能创建另一个 Session。崩溃会丢失尚未接纳提示词的规则调用。提示词接纳后，工作由普通 Session 日志、persistence、Workspace 与 Agent 生命周期拥有。

webhook 密钥只验证入站 GitHub 数据。它不会向规则代码或所创建 Agent 授予出站 GitHub 访问权；规则或 Agent 需要时应单独配置该权限。
