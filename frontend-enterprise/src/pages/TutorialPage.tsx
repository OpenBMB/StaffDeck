import { useEffect } from 'react';
import StaffdeckIcon, { type StaffdeckIconName } from '../components/StaffdeckIcon';

type TocGroup = {
  title: string;
  items: Array<{ id: string; label: string }>;
};

type Feature = {
  title: string;
  subtitle: string;
  body: string;
  icon: StaffdeckIconName;
  proof: string;
};

type QuickStep = {
  title: string;
  body: string;
  outcome: string;
};

type Scenario = {
  title: string;
  body: string;
  stack: string;
  tags: string[];
};

const TOC_GROUPS: TocGroup[] = [
  {
    title: '开始使用',
    items: [
      { id: 'intro', label: '项目简介' },
      { id: 'install', label: '安装说明' },
      { id: 'quickstart', label: '快速开始' },
    ],
  },
  {
    title: '核心功能',
    items: [
      { id: 'core-features', label: '能力总览' },
      { id: 'runtime', label: '运行闭环' },
      { id: 'governance', label: '治理与复盘' },
    ],
  },
  {
    title: '架构说明',
    items: [
      { id: 'architecture', label: '架构概览' },
      { id: 'flow', label: '执行流程' },
    ],
  },
  {
    title: '参考与案例',
    items: [
      { id: 'reference', label: '配置参考' },
      { id: 'development', label: '开发指南' },
      { id: 'showcase', label: '案例展示' },
      { id: 'faq', label: '常见问题' },
    ],
  },
];

const FEATURES: Feature[] = [
  {
    title: '数字员工',
    subtitle: '岗位边界',
    body: '每个员工独立维护岗位描述、服务范围、资源绑定和运营记录，适合把客服、导购、运营、知识助手拆成不同角色。',
    icon: 'user',
    proof: '档案 / 资源 / 权限',
  },
  {
    title: '知识库',
    subtitle: '可信来源',
    body: '把制度、商品、交付和服务口径解析为可检索片段，回复时保留引用线索，降低“听起来对但查不到来源”的风险。',
    icon: 'database',
    proof: '文档 / 桶 / 片段 / 引用',
  },
  {
    title: '技能',
    subtitle: '可复用能力',
    body: '把浏览器、查询、文档处理、MCP 或项目工作流沉淀为技能，让能力能被多个员工复用和迭代。',
    icon: 'spark',
    proof: '运行测试 / 版本 / 发布',
  },
  {
    title: 'SOP',
    subtitle: '流程约束',
    body: '用节点、必填信息、允许动作、中断策略和回复规则描述流程；已满足的信息不重复追问，只推进真正缺失的部分。',
    icon: 'filter',
    proof: '节点 / 槽位 / 动作白名单',
  },
  {
    title: '工具',
    subtitle: '业务动作',
    body: '通过 HTTP 工具和内置工具连接订单、商品、知识检索或内部服务，让员工可以查询、校验、创建和触发动作。',
    icon: 'tool',
    proof: 'Schema / 测试 / 调用日志',
  },
  {
    title: '记忆',
    subtitle: '长期上下文',
    body: '把用户偏好、项目背景、复盘结论和协作习惯沉淀为可查记录，帮助后续会话继承已验证的上下文。',
    icon: 'history',
    proof: '抽取 / 回忆 / 复用',
  },
  {
    title: '定时任务',
    subtitle: '后台常驻',
    body: '让员工按一次性、每日、每周或每月计划执行提示词，适合巡检、周报、提醒、异常跟进和周期分析。',
    icon: 'clock',
    proof: '计划 / 执行 / 历史',
  },
  {
    title: '追踪与反馈',
    subtitle: '运营闭环',
    body: '用对话日志、Trace、反馈分析和事件记录串起路由、工具调用、回复与改进线索，失败后能定位到具体环节。',
    icon: 'eye',
    proof: 'Trace / Feedback / Event',
  },
];

const QUICK_STEPS: QuickStep[] = [
  {
    title: '建立运行底座',
    body: '准备 OpenAI 兼容模型、单端口服务和 demo 租户数据，让企业端、对话端、API 文档在同一个本地入口下运行。',
    outcome: '系统可启动、页面可访问、模型可用于生成。',
  },
  {
    title: '定义一个真实岗位',
    body: '先选一个低风险但流程密集的岗位，例如售后服务、导购、运营巡检或内部知识助手。',
    outcome: '岗位边界清楚，知道它能处理什么、不能处理什么。',
  },
  {
    title: '补齐知识与 SOP',
    body: '把业务文档沉淀为知识库，把关键流程拆成节点、必填信息、允许动作和回复规则。',
    outcome: '员工能按业务规则推进，而不是自由发挥。',
  },
  {
    title: '连接必要工具',
    body: '只给员工接入它真正需要的工具，例如订单查询、商品购买、内部查询或知识检索。',
    outcome: '工具调用可验证，参数和结果都能复盘。',
  },
  {
    title: '用真实表达试跑',
    body: '不要只用标准话术测试，要用用户真实表达覆盖缺信息、信息已给全、改口、插话和转人工。',
    outcome: '流程能跳过已满足信息，并在关键动作前确认。',
  },
  {
    title: '把结果变成运营资产',
    body: '通过 Trace、反馈、记忆和定时任务沉淀失败原因、稳定口径和长期任务。',
    outcome: '一次测试能变成下一轮配置改进。',
  },
];

const SCENARIOS: Scenario[] = [
  {
    title: '售后服务员工',
    body: '退款、退货、换货流程需要确认订单、查询资格、收集原因并控制承诺边界，是最适合验证 SOP 和工具调用的场景。',
    stack: '售后 SOP -> 订单查询工具 -> 风险回复规则 -> Trace 复盘',
    tags: ['订单确认', '资格查询', '转人工'],
  },
  {
    title: '电商导购员工',
    body: '通过商品知识库、比价技能和购买工具，完成推荐、价格解释、下单确认和结果反馈。',
    stack: '商品知识库 -> 比价技能 -> 购买 SOP -> 工具结果反馈',
    tags: ['商品知识', '价格对比', '下单确认'],
  },
  {
    title: '运营常驻员工',
    body: '周期巡检、周报、异常提醒和待办跟进可以配置为定时任务，让员工在后台持续推进。',
    stack: '定时任务 -> 运行记录 -> 记忆沉淀 -> 下轮复用',
    tags: ['Cron', '周报', '持续跟踪'],
  },
  {
    title: '内部知识助手',
    body: '把制度、交付文档和服务规范沉淀为知识库，再用引用和反馈持续修正口径。',
    stack: '知识库 -> 引用回复 -> 反馈分析 -> 知识更新',
    tags: ['制度问答', '引用来源', '口径治理'],
  },
];

const ARCHITECTURE_LAYERS = [
  ['入口层', '对话端承接真实用户表达；企业端负责配置、运营和复盘。'],
  ['配置层', '模型、员工、知识库、技能、SOP、工具、定时任务共同定义员工能力。'],
  ['运行层', 'Router、Agent Loop、Skill Runtime 和 Response Generator 推进任务。'],
  ['上下文层', '知识引用、长期记忆、会话状态和反馈结果进入下一次决策。'],
  ['观测层', 'Trace、事件日志和反馈分析让每次执行都能被追踪和改进。'],
];

export default function TutorialPage() {
  useEffect(() => {
    const rawHash = window.location.hash.slice(1);
    if (!rawHash) return undefined;
    let targetId = rawHash;
    try {
      targetId = decodeURIComponent(rawHash);
    } catch {
      targetId = rawHash;
    }

    const frame = window.requestAnimationFrame(() => {
      const target = document.getElementById(targetId);
      if (!target) return;
      const top = target.getBoundingClientRect().top + window.scrollY - 24;
      const previousScrollBehavior = document.documentElement.style.scrollBehavior;
      document.documentElement.style.scrollBehavior = 'auto';
      window.scrollTo(0, Math.max(top, 0));
      document.documentElement.style.scrollBehavior = previousScrollBehavior;
    });

    return () => window.cancelAnimationFrame(frame);
  }, []);

  return (
    <main className="tutorial-doc-page">
      <section className="tutorial-doc-hero" id="intro">
        <div className="tutorial-doc-hero-copy">
          <span className="ui-typography tutorial-doc-eyebrow">StaffDeck Docs</span>
          <h1 className="ui-typography">企业数字员工运行时，从配置到持续运营</h1>
          <p className="ui-typography">
            StaffDeck 把模型、数字员工、知识库、技能、SOP、工具、记忆、定时任务和 Trace 放到一条链路里，
            让企业拥有可配置、可验证、可复盘的业务流程对话系统。
          </p>
          <div className="tutorial-doc-actions">
            <a className="tutorial-doc-primary-action" href="#quickstart">快速开始</a>
            <a className="tutorial-doc-secondary-action" href="#core-features">查看核心功能</a>
          </div>
          <div className="tutorial-doc-proof-row">
            <span><strong>8</strong>核心模块</span>
            <span><strong>6</strong>步运行闭环</span>
            <span><strong>4</strong>推荐场景</span>
          </div>
        </div>
        <div className="tutorial-doc-hero-map" aria-label="StaffDeck runtime map">
          <span className="tutorial-doc-map-label">Agent-native business runtime</span>
          <div className="tutorial-doc-map-grid">
            {FEATURES.slice(0, 6).map((feature) => (
              <span key={feature.title}>
                <StaffdeckIcon name={feature.icon} />
                <em>{feature.title}</em>
              </span>
            ))}
          </div>
          <div className="tutorial-doc-map-line">
            <strong>Conversation</strong>
            <i />
            <strong>Workflow</strong>
            <i />
            <strong>Operations</strong>
          </div>
        </div>
      </section>

      <div className="tutorial-doc-shell">
        <aside className="tutorial-doc-nav" aria-label="StaffDeck 单页文档目录">
          <div className="tutorial-doc-nav-title">
            <span>目录</span>
            <strong>页面章节</strong>
          </div>
          {TOC_GROUPS.map((group) => (
            <nav key={group.title}>
              <span>{group.title}</span>
              {group.items.map((item) => (
                <a key={item.id} href={`#${item.id}`}>{item.label}</a>
              ))}
            </nav>
          ))}
        </aside>

        <div className="tutorial-doc-main">

      <section className="tutorial-doc-section tutorial-doc-intro-panel">
        <div>
          <span className="ui-typography tutorial-doc-eyebrow">项目简介</span>
          <h2 className="ui-typography">不是通用 Agent 框架，而是面向业务流程的企业对话运行时</h2>
          <p className="ui-typography">
            StaffDeck 的核心不是“能聊天”，而是让一个真实岗位拥有自己的配置、资源、执行规则和运营记录。
            每个员工都可以有独立知识、SOP、工具和记忆；每次对话都能回看路由、工具调用、回复和反馈。
          </p>
        </div>
        <div className="tutorial-doc-pain-grid">
          <span>流程靠人盯</span>
          <span>知识口径漂移</span>
          <span>工具调用不可控</span>
          <span>失败无法复盘</span>
        </div>
      </section>

      <section className="tutorial-doc-section" id="install">
        <SectionHeading
          eyebrow="Getting Started"
          title="安装与入口"
          body="推荐单端口启动：企业端、对话端和 API 文档都由同一个 FastAPI 进程挂载，适合本地演示和外部隧道测试。"
        />
        <div className="tutorial-doc-install-grid">
          <div className="tutorial-doc-command-card">
            <span>一键启动</span>
            <code>scripts/dev_up.sh</code>
            <p>构建前端，并挂载对话端、企业端和 API。</p>
          </div>
          <div className="tutorial-doc-command-card">
            <span>后台运行</span>
            <code>DETACH=1 scripts/dev_up.sh</code>
            <p>适合浏览器验证和长时间演示。</p>
          </div>
          <div className="tutorial-doc-command-card">
            <span>查看状态</span>
            <code>scripts/dev_status.sh</code>
            <p>确认端口、健康检查和日志位置。</p>
          </div>
          <div className="tutorial-doc-command-card">
            <span>停止服务</span>
            <code>scripts/dev_down.sh</code>
            <p>停止脚本托管的本地进程。</p>
          </div>
        </div>
      </section>

      <section className="tutorial-doc-section" id="quickstart">
        <SectionHeading
          eyebrow="Quick Start"
          title="从空系统到一个可复盘员工"
          body="这不是单次 demo，而是一条最短运营闭环：配置、验证、复盘、沉淀。"
        />
        <div className="tutorial-doc-steps">
          {QUICK_STEPS.map((step, index) => (
            <article key={step.title} className="tutorial-doc-step">
              <em>{String(index + 1).padStart(2, '0')}</em>
              <div>
                <h3 className="ui-typography">{step.title}</h3>
                <p className="ui-typography">{step.body}</p>
              </div>
              <strong>{step.outcome}</strong>
            </article>
          ))}
        </div>
      </section>

      <section className="tutorial-doc-section" id="core-features">
        <SectionHeading
          eyebrow="Core Features"
          title="核心功能不是散点，而是一套员工运行系统"
          body="PilotDeck 把能力分成 WorkSpace、Router、Memory、Always On、Gateway；StaffDeck 对应到企业数字员工场景，重点是岗位边界、知识治理、流程执行、工具调用和运营复盘。"
        />
        <div className="tutorial-doc-feature-grid">
          {FEATURES.map((feature) => (
            <article key={feature.title} className="tutorial-doc-feature">
              <span><StaffdeckIcon name={feature.icon} /></span>
              <em>{feature.subtitle}</em>
              <strong>{feature.title}</strong>
              <p>{feature.body}</p>
              <small>{feature.proof}</small>
            </article>
          ))}
        </div>
      </section>

      <section className="tutorial-doc-section tutorial-doc-runtime" id="runtime">
        <SectionHeading
          eyebrow="Runtime Loop"
          title="一条业务请求如何被推进"
          body="StaffDeck 的对话不是单轮问答。它会在路由、知识、技能、SOP、工具和回复生成之间形成可追踪执行链路。"
        />
        <div className="tutorial-doc-loop">
          {['用户消息', 'Router 判断', 'SOP / 技能推进', '知识与工具调用', '回复生成', 'Trace / 反馈 / 记忆'].map((item) => (
            <span key={item}>{item}</span>
          ))}
        </div>
      </section>

      <section className="tutorial-doc-section" id="governance">
        <SectionHeading
          eyebrow="Operations"
          title="让 AI 员工可运营、可治理"
          body="真正能落地的企业 AI，需要知道它做了什么、为什么这么做、哪里失败、下一次如何改。"
        />
        <div className="tutorial-doc-governance-grid">
          {['对话日志记录每轮输入输出', 'Trace 展示路由、槽位和工具调用', '反馈分析定位失败原因', '记忆沉淀长期偏好和复盘结论', '定时任务把周期工作常驻化', '开放广场让能力复制和复用'].map((item) => (
            <span key={item}><StaffdeckIcon name="check" />{item}</span>
          ))}
        </div>
      </section>

      <section className="tutorial-doc-section" id="architecture">
        <SectionHeading
          eyebrow="Architecture"
          title="架构概览"
          body="当前仓库由后端服务、企业端控制台和对话端组成。企业端负责配置，对话端负责使用，后端运行对话、知识、工具和任务。"
        />
        <div className="tutorial-doc-architecture">
          {ARCHITECTURE_LAYERS.map(([title, body]) => (
            <article key={title}>
              <strong>{title}</strong>
              <p>{body}</p>
            </article>
          ))}
        </div>
      </section>

      <section className="tutorial-doc-section" id="flow">
        <SectionHeading
          eyebrow="Execution Flow"
          title="核心执行流程"
          body="从用户消息到最终回复，中间的每一步都应该能在日志和 Trace 中找到证据。"
        />
        <pre className="tutorial-doc-code">{`User message
  -> Chat API
  -> Router
  -> Skill / SOP / Knowledge / Tool
  -> Agent Loop
  -> Response Generator
  -> Trace / Feedback / Memory`}</pre>
      </section>

      <section className="tutorial-doc-section" id="reference">
        <SectionHeading
          eyebrow="Reference"
          title="配置参考"
          body="这些不是跳转入口，而是配置时应当检查的对象和验收标准。"
        />
        <div className="tutorial-doc-reference-grid">
          <span>模型：默认模型唯一，测试连接成功，密钥脱敏展示。</span>
          <span>员工：岗位边界明确，资源绑定清楚，状态可管理。</span>
          <span>知识：文档解析完成，能命中引用，口径可复查。</span>
          <span>SOP：节点、槽位、允许动作和回复规则明确。</span>
          <span>工具：参数 schema 可验证，测试调用有结果。</span>
          <span>运营：日志、Trace、反馈、记忆能形成闭环。</span>
        </div>
      </section>

      <section className="tutorial-doc-section" id="development">
        <SectionHeading
          eyebrow="Development"
          title="开发指南"
          body="功能开发时保持一个原则：配置变更要能被运行验证，运行失败要能被 Trace 解释。"
        />
        <div className="tutorial-doc-dev-grid">
          <div><code>scripts/dev_status.sh</code><span>查看当前服务状态</span></div>
          <div><code>scripts/dev_down.sh</code><span>停止单端口服务</span></div>
          <div><code>cd backend && .venv/bin/pytest</code><span>运行后端测试</span></div>
          <div><code>cd frontend-enterprise && npm run build</code><span>验证企业端构建</span></div>
        </div>
      </section>

      <section className="tutorial-doc-section" id="showcase">
        <SectionHeading
          eyebrow="Showcase"
          title="适合先试跑的企业场景"
          body="先从低风险、高流程密度、可复盘的任务开始，再逐步接入更强工具和更高风险动作。"
        />
        <div className="tutorial-doc-showcase-grid">
          {SCENARIOS.map((scenario) => (
            <article key={scenario.title}>
              <h3 className="ui-typography">{scenario.title}</h3>
              <p className="ui-typography">{scenario.body}</p>
              <code>{scenario.stack}</code>
              <div>
                {scenario.tags.map((tag) => <span key={tag} className="ui-tag">{tag}</span>)}
              </div>
            </article>
          ))}
        </div>
      </section>

      <section className="tutorial-doc-section" id="faq">
        <SectionHeading
          eyebrow="FAQ"
          title="常见问题排查"
          body="大多数问题可以落到模型、知识、SOP、工具、权限和 Trace 六个层面定位。"
        />
        <div className="tutorial-doc-faq">
          <details open>
            <summary>模型测试失败怎么办？</summary>
            <p>检查 API Key、Base URL 是否包含 `/v1`、模型 ID、余额和网络。先让默认模型测试通过，再验证对话端。</p>
          </details>
          <details>
            <summary>SOP 为什么重复追问？</summary>
            <p>检查节点 instruction、expected_user_info 和槽位抽取策略，确保已满足信息会被跳过。</p>
          </details>
          <details>
            <summary>工具没有被调用怎么办？</summary>
            <p>检查 SOP 节点 allowed_actions、工具启用状态、参数 schema 和当前员工绑定关系。</p>
          </details>
          <details>
            <summary>知识库没有引用怎么办？</summary>
            <p>检查文档解析状态、知识库是否绑定到当前员工，以及问题是否覆盖文档中的关键概念。</p>
          </details>
        </div>
      </section>
        </div>
      </div>
    </main>
  );
}

function SectionHeading({ eyebrow, title, body }: { eyebrow: string; title: string; body: string }) {
  return (
    <div className="tutorial-doc-section-heading">
      <span className="ui-typography tutorial-doc-eyebrow">{eyebrow}</span>
      <h2 className="ui-typography">{title}</h2>
      <p className="ui-typography">{body}</p>
    </div>
  );
}
