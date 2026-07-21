<div align="center">

<img src="packaging/assets/staffdeck_banner_cn.png" alt="StaffDeck 标志"  />

<p align="center">
  <a href="https://staffdeck.openbmb.cn/"><img src="https://img.shields.io/badge/Website-staffdeck.openbmb.cn-FF6B35?style=flat-square&logo=googlechrome&logoColor=white" alt="Official Website"/></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-AGPL_3.0-blue.svg?style=flat-square" alt="License"/></a>
  <a href="https://github.com/OpenBMB/StaffDeck/stargazers"><img src="https://img.shields.io/github/stars/OpenBMB/StaffDeck?style=flat-square" alt="Stars"/></a>
  <br/>
  <a href="#-联系我们"><img src="https://img.shields.io/badge/Discord-社群-5865F2?style=for-the-badge&logo=discord&logoColor=white" alt="Discord"/></a>
  &nbsp;
  <a href="#-联系我们"><img src="https://img.shields.io/badge/飞书-交流群-00D6B9?style=for-the-badge&logo=bytedance&logoColor=white" alt="Feishu"/></a>
  &nbsp;
  <a href="#-联系我们"><img src="https://img.shields.io/badge/微信-交流群-07C160?style=for-the-badge&logo=wechat&logoColor=white" alt="WeChat"/></a>
  <br/>
</p>

[English](./README.md) | **简体中文**


</div>


## 更新日志

  - **2026-07-15**：StaffDeck正式开源！欢迎大家使用反馈与Star支持。

# 💡 关于StaffDeck

StaffDeck是一套面向企业的数字员工构建与管理平台，帮助专业员工将工作经验、业务流程和判断标准固化为可以持续工作的数字员工，接手重复性任务，并将个人能力沉淀为可复用、可迭代、可追溯的组织资产。StaffDeck由[面壁智能](https://modelbest.cn/)，[东北大学-面壁智能数据智能联合实验室](https://neuir.github.io/)，[清华大学THUNLP实验室](https://nlp.csai.tsinghua.edu.cn/)，[OpenBMB](https://www.openbmb.cn/home)与[AI9Stars](https://github.com/AI9Stars)联合研发，面向希望将 AI 从个人效率工具升级为组织生产力的企业与机构。

## 核心亮点

- 🧑‍💼 **数字员工构建与管理**：将专业员工的经验、流程和判断标准固化为拥有岗位、工号、能力档案和工作记录的数字员工；支持能力成长、权限隔离及发布复用。
- 🧩 **状态机驱动的流程型技能**：通过自然语言生成结构化 SOP，以状态机保证复杂流程准确执行；支持多个流程实时切换、上下文保留、可视化编辑、版本管理和分支演化。
- 📚 **文档结构感知的知识检索**：基于文档、章节、页面和摘要等层级构建可导航索引，让数字员工先判断信息可能位于哪里，再逐层定位原文；支持知识分桶、定向检索、来源引用和检索调试。
- 🔌 **自主执行与持续迭代**：通过 HTTP API、MCP 和定时任务执行真实业务操作，并结合长期记忆、完整 Trace、真人接管、用户反馈和反馈分析形成持续迭代闭环。

## 客户端下载

访问 [StaffDeck 官方网站](https://staffdeck.openbmb.cn/)，或直接下载最新桌面客户端：

| 平台 | 架构 | 下载 |
| --- | --- | --- |
| macOS | Apple Silicon（arm64） | [下载 `.dmg`](https://github.com/OpenBMB/StaffDeck/releases/latest/download/StaffDeck-macos-arm64.dmg) |
| Windows | x64 | [下载安装程序 `.exe`](https://github.com/OpenBMB/StaffDeck/releases/latest/download/StaffDeck-windows-x64-setup.exe) |
| Linux | x86_64（Debian/Ubuntu） | [下载 `.deb`](https://github.com/OpenBMB/StaffDeck/releases/latest/download/StaffDeck-linux-x86_64.deb) |

## Agent 一键部署

将下面的 Prompt 粘贴给 Cursor、Claude Code 或 Codex：

```text
阅读 https://raw.githubusercontent.com/OpenBMB/StaffDeck/main/README.zh.md。
克隆 OpenBMB/StaffDeck 私有仓库，准备 Python 3.11 或更高版本和 Node.js 20，创建
backend/.venv，安装前后端依赖，将 backend/.env.example 复制为
backend/.env；缺少 OpenAI 兼容模型地址或 API Key 时向我询问，并严格使用当前
系统对应的文档命令。macOS/Linux/WSL 运行 scripts/dev_up.sh --detach，Windows
PowerShell 运行 .\scripts\dev_up.ps1 --detach；验证 /api/health 和
/workspace/gallery 后再报告完成。
```


## 目录

- [💡 关于StaffDeck](#-关于staffdeck)
  - [核心亮点](#核心亮点)
  - [客户端下载](#客户端下载)
  - [Agent 一键部署](#agent-一键部署)
  - [目录](#目录)
  - [快速开始](#快速开始)
    - [环境要求](#环境要求)
    - [1. 克隆并安装](#1-克隆并安装)
    - [2. 配置模型](#2-配置模型)
    - [3. 启动 Web Demo](#3-启动-web-demo)
    - [4. 验证安装](#4-验证安装)
    - [常用命令](#常用命令)
      - [统一 Python 入口](#统一-python-入口)
  - [核心流程](#核心流程)
  - [项目结构](#项目结构)
  - [常见问题](#常见问题)
  - [路线图](#路线图)
- [💬 联系我们](#-联系我们)
  - [参与贡献](#参与贡献)
  - [风险与限制](#风险与限制)
  - [引用](#引用)
  - [许可证](#许可证)
  - [致谢](#致谢)

## 快速开始

### 环境要求

- 支持 macOS、Linux、WSL 或 Windows PowerShell
- Python **3.11+**
- Node.js **20+** 与 npm
- OpenAI Chat Completions 兼容的模型接口和 API Key
- 应用本身不要求 CUDA；硬件要求由所选择的模型服务决定

### 1. 克隆并安装

首先克隆仓库：

```bash
git clone https://github.com/OpenBMB/StaffDeck.git
cd StaffDeck
```

macOS、Linux 或 WSL：

```bash
python3 -m venv backend/.venv
backend/.venv/bin/python -m pip install -e "backend[dev]"
npm --prefix frontend-enterprise ci
cp backend/.env.example backend/.env
```

Windows PowerShell：

```powershell
py -3 -m venv backend\.venv
.\backend\.venv\Scripts\python.exe -m pip install -e "backend[dev]"
npm --prefix frontend-enterprise ci
Copy-Item backend\.env.example backend\.env
```

### 2. 配置模型

首次启动前编辑 `backend/.env`：

```dotenv
APP_SECRET="请替换为足够长的随机字符串"
DEMO_MODEL_BASE_URL="https://你的OpenAI兼容接口/v1"
DEMO_MODEL_NAME="你的模型名"
DEMO_MODEL_API_KEY="你的API-Key"
```

API Key 用于创建初始模型配置，存入数据库前会被加密。请勿提交 `backend/.env`。服务启动后也可以在**管理员 → 模型配置**中管理模型服务。

### 3. 启动 Web Demo

| 平台 | 推荐命令 |
| --- | --- |
| macOS、Linux 或 WSL | `scripts/dev_up.sh --detach` |
| Windows PowerShell | `.\scripts\dev_up.ps1 --detach` |

两套包装脚本最终都会调用同一个跨平台 Python 生命周期入口 `scripts/dev.py`。启动过程会构建 StaffDeck 前端，并由一个 FastAPI 进程在 `5173` 端口同时提供 UI、API 与 Swagger 文档。默认管理员账号为 `admin` / `admin`，请在首次登录后通过账号配置修改密码。

### 4. 验证安装

macOS、Linux 或 WSL：

```bash
curl http://127.0.0.1:5173/api/health
```

Windows PowerShell：

```powershell
curl.exe http://127.0.0.1:5173/api/health
```

预期输出：

```json
{"status":"ok"}
```

打开 [http://127.0.0.1:5173/workspace/gallery](http://127.0.0.1:5173/workspace/gallery)，选择一个数字员工并发送首条消息。回答和执行记录应该在同一个对话轮次中流式显示。

### 常用命令

| 操作 | macOS、Linux 或 WSL | Windows PowerShell |
| --- | --- | --- |
| 后台启动 | `scripts/dev_up.sh --detach` | `.\scripts\dev_up.ps1 --detach` |
| 前台启动 | `scripts/dev_up.sh` | `.\scripts\dev_up.ps1` |
| 查看服务状态 | `scripts/dev_status.sh` | `.\scripts\dev_status.ps1` |
| 停止本地服务 | `scripts/dev_down.sh` | `.\scripts\dev_down.ps1` |

#### 统一 Python 入口

上述包装脚本最终都会调用 `scripts/dev.py`。也可以直接使用第 1 步创建的项目虚拟环境，避免依赖 Shell 脚本执行能力或系统 Python Launcher：

| 平台 | 直接后台启动 |
| --- | --- |
| macOS、Linux 或 WSL | `backend/.venv/bin/python scripts/dev.py up --detach` |
| Windows PowerShell | `.\backend\.venv\Scripts\python.exe scripts\dev.py up --detach` |

需要执行其他操作时，将 `up --detach` 替换为对应的生命周期参数：

| 操作 | 参数 |
| --- | --- |
| 后台启动 | `up --detach` |
| 前台启动 | `up` |
| 查看服务状态 | `status` |
| 停止本地服务 | `down` |

> 完整说明 → [StaffDeck 使用教程](https://staffdeck.openbmb.cn/#/docs/introduce?lang=zh)




## 核心流程

1. **创建数字员工**：设置职位、岗位边界、服务风格、创建者与访问范围。
2. **配置员工能力**：从广场复制或自行创建知识库、通用技能、SOP 与工具，不修改广场原件。
3. **发起会话**：从数字员工广场或员工列表进入；发送首条消息后持久化正式 Session。
4. **执行并观测**：在执行记录中查看流式意图、检索、技能、工具、校验和回答事件。
5. **必要时介入**：继续排队请求、取消运行、转人工或处理待回答内容。
6. **持续运营**：利用记忆、反馈、对话日志和定时任务长期优化员工能力。

## 项目结构

```text
StaffDeck/
├── backend/                  # FastAPI 接口、Agent 运行时、存储与任务 Worker
├── frontend-enterprise/      # React/TypeScript StaffDeck 工作台
├── docs/                     # 教程、API、Schema 与示例流程
├── scripts/                  # 单端口服务生命周期与校验脚本
├── packaging/                # macOS、Linux 与 Windows 打包资源
├── README.md                 # English
└── README.zh.md              # 简体中文
```


## 常见问题

<details>
<summary><strong>页面可以打开，但数字员工不回答。</strong></summary>

检查所选模型配置、API Key、模型名和模型服务网络。随后查看执行记录与 `.dev/logs/app.log`，定位模型服务返回的具体错误。
</details>

<details>
<summary><strong>没有本地 GPU 可以运行吗？</strong></summary>

可以。应用调用 OpenAI 兼容模型接口，GPU 要求由你自行部署或使用的模型服务决定。
</details>

<details>
<summary><strong>为什么普通用户可以使用广场资源，但不能编辑？</strong></summary>

广场资源是可复用模板。普通用户可将有权限的资源复制或绑定到自己的员工，原始资源仍由创建者与管理员权限保护。
</details>

## 路线图

- [ ] 群聊，多数字员工沟通/分工
- [ ] 更多企业连接器与经过审核的广场资源
- [ ] 面向高风险工具动作的细粒度审批策略

路线优先级由真实部署需求驱动。请通过 [Issue](https://github.com/OpenBMB/StaffDeck/issues) 提供可复现的场景和预期行为。

# 💬 联系我们
- 关于技术问题及功能请求，请提交 [GitHub Issues](https://github.com/OpenBMB/StaffDeck/issues)。
- 商业合作，请联系：
  ```
  business@modelbest.cn
  ```
- 欢迎加入我们的社区与我们交流：

<table width="100%">
<tr>
<td width="33%" align="center"><b>微信交流群</b></td>
<td width="33%" align="center"><b>飞书交流群</b></td>
<td width="33%" align="center"><b>Discord 社区</b></td>
</tr>
<tr>
<td align="center"><img src="packaging/assets/qr-wechat.png" width="200" alt="微信二维码"/></td>
<td align="center"><img src="packaging/assets/qr-feishu.jpg" width="200" alt="飞书二维码"/></td>
<td align="center"><img src="packaging/assets/qr-discord.png" width="200" alt="Discord 二维码"/></td>
</tr>
</table>

## 参与贡献

欢迎获得仓库权限的协作者参与：

- 提交可复现的 Bug 与权限问题
- 提议数字员工、知识、技能、SOP 或工具流程
- 提交范围清晰、包含测试与浏览器校验的 PR
- 改进文档和中英翻译

请保留工作区中与任务无关的修改，根据影响范围补充测试，并在 PR 中写明完成 UI 校验的路由与用户角色。

## 风险与限制

- 模型回答可能不正确、不完整或不一致；执行记录可以提高可审计性，但不能保证结论正确。
- 知识检索效果受原始文档质量、解析、索引、权限与模型能力共同影响。
- 外部工具与生成的 Runner 可能产生真实副作用。应使用最小权限凭据，并为高风险动作配置人工审批。
- 定时任务依赖持续运行的 Worker 与正确的用户时区设置。
- 本项目不能替代法律、医疗、金融、安全及其他受监管领域的专业审核。
- 未获得适当授权、隐私保护与人工监督时，不得使用本平台处理数据或自动作出重要决定。

## 引用

在内部研究或经授权的公开材料中使用 StaffDeck 时，可引用：

```bibtex
@software{StaffDeck2026,
  title  = {StaffDeck: Build, Run, and Govern Enterprise Digital Employees},
  author = {OpenBMB},
  year   = {2026},
  url    = {https://github.com/OpenBMB/StaffDeck}
}
```
## Star 历史

<a href="https://www.star-history.com/?repos=openbmb%2Fstaffdeck&type=date&legend=top-left">
 <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=openbmb/staffdeck&type=date&theme=dark&legend=top-left&sealed_token=lLohLC57bGAPh4lrzSYu2xW6Fmkavbj5r-T25GGt-jA10veIrv9OBPs0wiE5A98VIxP0NyxjbloW1t5OnPdVn6RT_L6Dmsp5EnfiWsGirs6G3Bv5_l_zUw" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=openbmb/staffdeck&type=date&legend=top-left&sealed_token=lLohLC57bGAPh4lrzSYu2xW6Fmkavbj5r-T25GGt-jA10veIrv9OBPs0wiE5A98VIxP0NyxjbloW1t5OnPdVn6RT_L6Dmsp5EnfiWsGirs6G3Bv5_l_zUw" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=openbmb/staffdeck&type=date&legend=top-left&sealed_token=lLohLC57bGAPh4lrzSYu2xW6Fmkavbj5r-T25GGt-jA10veIrv9OBPs0wiE5A98VIxP0NyxjbloW1t5OnPdVn6RT_L6Dmsp5EnfiWsGirs6G3Bv5_l_zUw" />
 </picture>
</a>

## 许可证

本项目基于 GNU Affero General Public License v3.0 开源。

## 致谢

StaffDeck 由 [OpenBMB](https://www.openbmb.cn/) 生态孵化。
