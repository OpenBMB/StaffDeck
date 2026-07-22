<div align="center">

<img src="packaging/assets/staffdeck_banner_en.png" alt="StaffDeck logo" />

<p align="center">
  <a href="https://staffdeck.openbmb.cn/"><img src="https://img.shields.io/badge/Website-staffdeck.openbmb.cn-FF6B35?style=flat-square&logo=googlechrome&logoColor=white" alt="Official Website"/></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-AGPL_3.0-blue.svg?style=flat-square" alt="License"/></a>
  <a href="https://github.com/OpenBMB/StaffDeck/stargazers"><img src="https://img.shields.io/github/stars/OpenBMB/StaffDeck?style=flat-square" alt="Stars"/></a>
  <br/>
  <a href="#-Community"><img src="https://img.shields.io/badge/Discord-Join_Community-5865F2?style=for-the-badge&logo=discord&logoColor=white" alt="Discord"/></a>
  &nbsp;
  <a href="#-Community"><img src="https://img.shields.io/badge/Feishu-Community-00D6B9?style=for-the-badge&logo=bytedance&logoColor=white" alt="Feishu"/></a>
  &nbsp;
  <a href="#-Community"><img src="https://img.shields.io/badge/WeChat-Community-07C160?style=for-the-badge&logo=wechat&logoColor=white" alt="WeChat"/></a>
  <br/>
</p>

**English** | [简体中文](./README.zh.md)
</div>

## News

- 📌 **Pinned · 2026-07-15**: StaffDeck is now open source! We welcome your feedback and support with a Star.

# 💡 About StaffDeck

StaffDeck is an enterprise platform for building and managing digital employees. It helps professionals turn their work experience, business processes, and decision criteria into digital employees that can operate continuously, take over repetitive tasks, and preserve individual expertise as reusable, evolvable, and traceable organizational assets. StaffDeck is jointly developed by the [ModelBest](https://modelbest.cn/), [NEU-ModelBest Data Intelligence Joint Lab](https://neuir.github.io/), [THUNLP](https://nlp.csai.tsinghua.edu.cn/), [OpenBMB](https://www.openbmb.cn/home), and [AI9Stars](https://github.com/AI9Stars) for enterprises and institutions seeking to advance AI from a personal productivity tool to an organizational capability.

## Core Features

- 🧑‍💼 **Build and manage digital employees**: Turn professional experience, processes, and decision criteria into digital employees with positions, employee IDs, capability profiles, and work records. Support capability growth, permission isolation, publishing, and reuse.
- 🧩 **State-machine-driven procedural skills**: Generate structured SOPs from natural language and use state machines to execute complex processes accurately. Support real-time switching across multiple flows, context preservation, visual editing, version management, and branch evolution.
- 📚 **Document-structure-aware knowledge retrieval**: Build navigable indexes across documents, chapters, pages, summaries, and other levels, allowing digital employees to first estimate where information may reside and then locate the original text step by step. Support knowledge buckets, targeted retrieval, source citations, and retrieval debugging.
- 🔌 **Autonomous execution and continuous improvement**: Perform real business operations through HTTP APIs, MCP, and scheduled tasks, then close the improvement loop with long-term memory, complete traces, human takeover, user feedback, and feedback analysis.

## Desktop Downloads

Visit the [StaffDeck official website](https://staffdeck.openbmb.cn/) or download the latest desktop release directly:

| Platform | Architecture | Download |
| --- | --- | --- |
| macOS | Apple Silicon (arm64) | [Download `.dmg`](https://github.com/OpenBMB/StaffDeck/releases/latest/download/StaffDeck-macos-arm64.dmg) |
| Windows | x64 | [Download installer `.exe`](https://github.com/OpenBMB/StaffDeck/releases/latest/download/StaffDeck-windows-x64-setup.exe) |
| Linux | x86_64 (Debian/Ubuntu) | [Download `.deb`](https://github.com/OpenBMB/StaffDeck/releases/latest/download/StaffDeck-linux-x86_64.deb) |

## Agent-Friendly Quick Deploy

Paste the prompt below into Cursor, Claude Code, or Codex:

```text
Read https://raw.githubusercontent.com/OpenBMB/StaffDeck/main/README.md.
Clone the OpenBMB/StaffDeck repository, prepare Python 3.11 or newer and Node.js 20,
create backend/.venv, install the backend and frontend dependencies, copy
backend/.env.example to backend/.env, ask me for the OpenAI-compatible model
endpoint and API key if they are missing, and use the commands documented for
the current OS. Start with scripts/dev_up.sh --detach on macOS/Linux/WSL or
.\scripts\dev_up.ps1 --detach on Windows PowerShell, then verify /api/health
plus /workspace/gallery before reporting success.
```


## Table of Contents

- [💡 About StaffDeck](#-about-staffdeck)
  - [Core Features](#core-features)
  - [Desktop Downloads](#desktop-downloads)
  - [Agent-Friendly Quick Deploy](#agent-friendly-quick-deploy)
  - [Table of Contents](#table-of-contents)
  - [Quick Start](#quick-start)
    - [Requirements](#requirements)
    - [1. Clone and Install](#1-clone-and-install)
    - [2. Configure a Model](#2-configure-a-model)
    - [3. Launch the Web Demo](#3-launch-the-web-demo)
    - [4. Verify the Installation](#4-verify-the-installation)
    - [Useful Commands](#useful-commands)
      - [Unified Python Entry](#unified-python-entry)
  - [Core Workflows](#core-workflows)
  - [Project Structure](#project-structure)
  - [FAQ](#faq)
  - [Roadmap](#roadmap)
- [💬 Community](#-community)
  - [Contributing](#contributing)
  - [Risks and Limitations](#risks-and-limitations)
  - [Citation](#citation)
  - [License](#license)
  - [Acknowledgments](#acknowledgments)

## Quick Start

### Requirements

- macOS, Linux, WSL, or Windows PowerShell
- Python **3.11+**
- Node.js **20+** and npm
- An OpenAI-compatible Chat Completions endpoint and API key
- No CUDA requirement for the application itself; hardware requirements depend on the selected model service

### 1. Clone and Install

Clone the repository first:

```bash
git clone https://github.com/OpenBMB/StaffDeck.git
cd StaffDeck
```

On macOS, Linux, or WSL:

```bash
python3 -m venv backend/.venv
backend/.venv/bin/python -m pip install -e "backend[dev]"
npm --prefix frontend-enterprise ci
cp backend/.env.example backend/.env
```

On Windows PowerShell:

```powershell
py -3 -m venv backend\.venv
.\backend\.venv\Scripts\python.exe -m pip install -e "backend[dev]"
npm --prefix frontend-enterprise ci
Copy-Item backend\.env.example backend\.env
```

### 2. Configure a Model

Edit `backend/.env` before the first startup:

```dotenv
APP_SECRET="replace-with-a-long-random-secret"
DEMO_MODEL_BASE_URL="https://your-openai-compatible-endpoint/v1"
DEMO_MODEL_NAME="your-model-name"
DEMO_MODEL_API_KEY="your-api-key"
```

The API key is used to create the initial model configuration and is encrypted before being stored in the database. Do not commit `backend/.env`. After startup, model services can also be managed from **Admin → Model Configuration**.

### 3. Launch the Web Demo

| Platform | Recommended command |
| --- | --- |
| macOS, Linux, or WSL | `scripts/dev_up.sh --detach` |
| Windows PowerShell | `.\scripts\dev_up.ps1 --detach` |

Both wrappers call the same cross-platform Python lifecycle entry, `scripts/dev.py`. The startup process builds the StaffDeck frontend and serves the UI, API, and Swagger documentation from one FastAPI process on port `5173`.

Initial administrator credentials: username `admin`, password `admin`. Please change the password after first login.

### 4. Verify the Installation

On macOS, Linux, or WSL:

```bash
curl http://127.0.0.1:5173/api/health
```

On Windows PowerShell:

```powershell
curl.exe http://127.0.0.1:5173/api/health
```

Expected output:

```json
{"status":"ok"}
```

Open [http://127.0.0.1:5173/workspace/gallery](http://127.0.0.1:5173/workspace/gallery), select a digital employee, and send the first message. The answer and its execution record should stream into the same conversation turn.

### Useful Commands

| Action | macOS, Linux, or WSL | Windows PowerShell |
| --- | --- | --- |
| Start in the background | `scripts/dev_up.sh --detach` | `.\scripts\dev_up.ps1 --detach` |
| Start in the foreground | `scripts/dev_up.sh` | `.\scripts\dev_up.ps1` |
| Inspect service status | `scripts/dev_status.sh` | `.\scripts\dev_status.ps1` |
| Stop the local service | `scripts/dev_down.sh` | `.\scripts\dev_down.ps1` |

#### Unified Python Entry

The wrapper scripts above delegate to `scripts/dev.py`. It can also be called directly with the project virtual environment created in step 1, avoiding any dependency on shell script execution or a system Python launcher:

| Platform | Direct background start |
| --- | --- |
| macOS, Linux, or WSL | `backend/.venv/bin/python scripts/dev.py up --detach` |
| Windows PowerShell | `.\backend\.venv\Scripts\python.exe scripts\dev.py up --detach` |

Replace `up --detach` with another lifecycle argument when needed:

| Action | Arguments |
| --- | --- |
| Start in the background | `up --detach` |
| Start in the foreground | `up` |
| Inspect service status | `status` |
| Stop the local service | `down` |

> Full guide → [StaffDeck Tutorial](https://staffdeck.openbmb.cn/#/docs/introduce?lang=en)




## Core Workflows

1. **Create a digital employee**: Define the position, role boundaries, service style, creator, and access scope.
2. **Configure employee capabilities**: Copy from the marketplace or create knowledge bases, general skills, SOPs, and tools without modifying marketplace originals.
3. **Start a session**: Enter from the digital employee marketplace or employee list; the formal session is persisted after the first message is sent.
4. **Execute and observe**: Inspect streaming intent, retrieval, skill, tool, review, and response events in the execution record.
5. **Intervene when necessary**: Continue with queued requests, cancel a run, hand work to a person, or process pending answers.
6. **Operate continuously**: Improve employee capabilities over time through memory, feedback, conversation logs, and scheduled tasks.

## Project Structure

```text
StaffDeck/
├── backend/                  # FastAPI APIs, agent runtime, storage, and task workers
├── frontend-enterprise/      # React/TypeScript StaffDeck workspace
├── docs/                     # Tutorials, APIs, schemas, and example flows
├── scripts/                  # Single-port service lifecycle and validation scripts
├── packaging/                # macOS, Linux, and Windows packaging assets
├── README.md                 # English
└── README.zh.md              # Simplified Chinese
```


## FAQ

<details>
<summary><strong>The page opens, but the digital employee does not answer.</strong></summary>

Check the selected model configuration, API key, model name, and model service network. Then inspect the execution record and `.dev/logs/app.log` to identify the exact error returned by the model service.
</details>

<details>
<summary><strong>Can StaffDeck run without a local GPU?</strong></summary>

Yes. The application calls an OpenAI-compatible model endpoint, so GPU requirements depend on the model service you deploy or use.
</details>

<details>
<summary><strong>Why can regular users use marketplace resources but not edit them?</strong></summary>

Marketplace resources are reusable templates. Regular users can copy or bind authorized resources to their own employees, while the original resources remain protected by creator and administrator permissions.
</details>

## Roadmap

- [ ] Group chat, multi-digital-employee communication, and task division
- [ ] More enterprise connectors and reviewed marketplace resources
- [ ] Fine-grained approval policies for high-risk tool actions

Roadmap priorities are driven by real deployment needs. Please open an [Issue](https://github.com/OpenBMB/StaffDeck/issues) with a reproducible scenario and expected behavior.

# 💬 Community
- For bugs and feature requests, please open a [GitHub Issues](https://github.com/OpenBMB/StaffDeck/issues)。
- For business corporation, please contact:
  ```
  business@modelbest.cn
  ```
- Join our community channels:

<table width="100%">
<tr>
<td width="33%" align="center"><b>WeChat Community</b></td>
<td width="33%" align="center"><b>Feishu Community</b></td>
<td width="33%" align="center"><b>Discord Community</b></td>
</tr>
<tr>
<td align="center"><img src="packaging/assets/qr-wechat.png" width="200" alt="微信二维码"/></td>
<td align="center"><img src="packaging/assets/qr-feishu.jpg" width="200" alt="飞书二维码"/></td>
<td align="center"><img src="packaging/assets/qr-discord.png" width="200" alt="Discord 二维码"/></td>
</tr>
</table>


## Contributing

Contributions from collaborators with repository access are welcome:

- Submit reproducible bugs and permission issues
- Propose digital employee, knowledge, skill, SOP, or tool workflows
- Submit focused pull requests with tests and browser validation
- Improve documentation and Chinese/English translations

Keep unrelated worktree changes intact, add tests proportional to the affected behavior, and state the routes and user roles used for UI verification in each pull request.

## Risks and Limitations

- Model responses can be incorrect, incomplete, or inconsistent. Execution records improve auditability but do not guarantee correctness.
- Knowledge retrieval quality depends on source-document quality, parsing, indexing, permissions, and model capabilities.
- External tools and generated runners can have real side effects. Use least-privilege credentials and configure human approval for high-risk actions.
- Scheduled tasks depend on a continuously running worker and correct user time-zone settings.
- This project is not a substitute for professional review in legal, medical, financial, security, or other regulated fields.
- Do not use this platform to process data or automate important decisions without appropriate authorization, privacy protection, and human oversight.

## Citation

When using StaffDeck in internal research or authorized public materials, cite:

```bibtex
@software{StaffDeck2026,
  title  = {StaffDeck: Build, Run, and Govern Enterprise Digital Employees},
  author = {OpenBMB},
  year   = {2026},
  url    = {https://github.com/OpenBMB/StaffDeck}
}
```
## Star History

<a href="https://www.star-history.com/?repos=openbmb%2Fstaffdeck&type=date&legend=top-left">
 <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=openbmb/staffdeck&type=date&theme=dark&legend=top-left&sealed_token=lLohLC57bGAPh4lrzSYu2xW6Fmkavbj5r-T25GGt-jA10veIrv9OBPs0wiE5A98VIxP0NyxjbloW1t5OnPdVn6RT_L6Dmsp5EnfiWsGirs6G3Bv5_l_zUw" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=openbmb/staffdeck&type=date&legend=top-left&sealed_token=lLohLC57bGAPh4lrzSYu2xW6Fmkavbj5r-T25GGt-jA10veIrv9OBPs0wiE5A98VIxP0NyxjbloW1t5OnPdVn6RT_L6Dmsp5EnfiWsGirs6G3Bv5_l_zUw" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=openbmb/staffdeck&type=date&legend=top-left&sealed_token=lLohLC57bGAPh4lrzSYu2xW6Fmkavbj5r-T25GGt-jA10veIrv9OBPs0wiE5A98VIxP0NyxjbloW1t5OnPdVn6RT_L6Dmsp5EnfiWsGirs6G3Bv5_l_zUw" />
 </picture>
</a>

## License

This project is open source under the GNU Affero General Public License v3.0.

## Acknowledgments

StaffDeck is incubated by the [OpenBMB](https://www.openbmb.cn/) ecosystem.
