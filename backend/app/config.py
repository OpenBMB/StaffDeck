import json
import os as _os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Skill Agent Loop Service"
    database_url: str = "sqlite:///./skill_agent_loop.db"
    app_secret: str = "change-me-in-development"
    demo_model_base_url: str = "http://localhost:52010/v1"
    demo_model_name: str = "qwen3.6-27b"
    demo_model_api_key: str = ""
    model_api_timeout_seconds: float = 600.0
    # 新建/编辑模型时可选用的固定模型列表（JSON 数组字符串）。
    # 例如：MODEL_PRESETS='["gpt-4o","gpt-4o-mini","qwen-max","qwen-plus","deepseek-v3","claude-sonnet-4-5","gemini-2.5-pro"]'
    # 未配置时使用下方默认内置列表，保证前端下拉始终有可选值。
    model_presets: str = '["qwen3.7-plus", "deepseek-v4.1-flash", "qwen3.8-27b"]'
    # 消费组「归属」业务字段的可选值（JSON 数组字符串），与阿里云接口无关。
    # 例如：CONSUMER_GROUP_OWNERS='["重庆项目","复星总部IT","Club Med"]'
    # 未配置时使用下方默认内置列表，保证前端下拉始终有可选值。
    consumer_group_owners: str = '["重庆项目", "总部IT", "Club Med"]'
    model_thinking_mode: str = ""
    model_thinking_models: str = ""
    # Laya 决策服务（Convai Laya Decision API，非自回归决策头）。前端「决策助手」
    # 一律经后端代理调用：该服务未挂 CORS 中间件（OPTIONS 直接 405），浏览器直连
    # 会被预检拦掉；同时地址属服务端内网，不应出现在前端产物里。
    laya_predict_url: str = "http://8.153.146.109:8080/predict"
    laya_timeout_seconds: float = 60.0
    # ---- Laya 决策头接入 SOP 走向决策 ----
    # 默认关闭：不开启时场景 L 的 guard 不通过，运行路径与改造前完全一致。
    laya_sop_routing_enabled: bool = False
    # P0 影子模式：只计算并记录 Laya 的判定，不真正改变路由（用于先看一致率）。
    laya_sop_shadow_mode: bool = True
    # choice/score 赢家概率、noul 的 max(p,1-p)，低于该值一律回退主模型。
    laya_min_confidence: float = 0.7
    # 路由专用短超时：与页面「决策助手」的 60s 分开，避免拖慢对话链路。
    laya_sop_timeout_seconds: float = 6.0
    # 连续失败达到该次数后本帧熔断，后续节点直接回退 LLM。
    laya_sop_circuit_breaker_failures: int = 3
    # ---- Laya 决策头接入知识路由（选文档 / 选桶，P2'）----
    # 默认关闭：不开启时知识检索行为与改造前逐字节一致。
    # 优先级固定为「缓存 / 小候选短路 / 词法 fast path > Laya > 现有 LLM」。
    laya_knowledge_route_enabled: bool = False
    # P0 影子模式：照常计算并记录 Laya 的候选判定，但不采用（用于看与 LLM 的一致率）。
    laya_knowledge_shadow_mode: bool = True
    # 候选数超过该值时直接回退 LLM：Laya 逐候选 noul 需 ceil(N/30) 次调用，
    # 多跳可能比一次 LLM 路由还慢。
    laya_knowledge_max_candidates: int = 30
    # 单次 /predict 携带的问题数上限（≤ 上游 MAX_QUESTIONS=30）。
    laya_knowledge_questions_per_call: int = 30
    # 知识路由专用短超时，与 SOP 旁路、页面「决策助手」60s 各自独立。
    laya_knowledge_timeout_seconds: float = 6.0
    # 对外可访问的站点基址（`PUBLIC_BASE_URL`），用于生成开放 API 的调用示例。
    # 部署在域名 / 公网 IP 后面时填它，决策助手页面「API 接入」里的示例就会用该地址；
    # 留空则前端回退成浏览器当前 origin（本地开发即 http://127.0.0.1:5173）。
    public_base_url: str = ""
    tool_timeout_seconds: float = 8.0
    a2a_task_timeout_seconds: float = 600.0
    a2a_poll_interval_seconds: float = 0.5
    codex_a2a_enabled: bool = False
    codex_a2a_command: str = "codex"
    codex_a2a_workspace_root: str = ""
    codex_a2a_timeout_seconds: float = 1800.0
    codex_a2a_token: str = ""
    tool_base_url: str = "http://localhost:5173"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    general_skill_runtime_python: str = ""
    general_skill_runtime_venv: str = ""
    general_skill_runtime_packages: str = "requests,httpx"
    # Keep runtime dependency installation enabled so published skills can
    # provision their declared baseline libraries on first use. Deployments
    # can still disable it explicitly for locked-down environments.
    general_skill_runtime_auto_install: bool = True
    general_skill_pip_index_url: str = ""
    general_skill_pip_timeout_seconds: int = 180
    general_skill_network_install: bool = True
    public_api_enabled: bool = True
    public_api_key_pepper: str = ""
    public_api_idempotency_ttl_seconds: int = 60 * 60 * 24
    public_api_retention_days: int = 30
    public_api_webhook_timeout_seconds: float = 10.0
    public_api_webhook_max_attempts: int = 6
    # ---- LDAP / AD 域登录 ----
    # 开启后登录优先走域认证：先用服务账号检索用户 DN，再用该 DN + 用户口令 bind 校验，
    # 成功后把域账号信息（显示名/邮箱/部门）同步到本地 users 表（无则新建、有则更新）。
    ldap_enabled: bool = False
    # 域控地址，形如 ldap://10.58.140.10:389 或 ldaps://10.58.140.10:636
    ldap_server_url: str = ""
    # 检索根：多个 OU 用 "|" 分隔，如 "OU=A,DC=fosun,DC=com|OU=B,DC=fosun,DC=com"
    ldap_base_dns: str = ""
    # 域名（fosun.com）：无服务账号时用 userPrincipalName（user@domain）直连 bind
    ldap_domain: str = ""
    # 服务账号（建议配置，用于检索用户 DN）；留空时回退为 UPN 直连 bind
    ldap_bind_dn: str = ""
    ldap_bind_password: str = ""
    # 用户检索过滤模板，{username} 占位（默认支持 sAMAccountName / userPrincipalName / mail 三种登录名）
    ldap_user_filter: str = "(|(sAMAccountName={username})(userPrincipalName={username})(mail={username}))"
    ldap_attr_username: str = "sAMAccountName"
    ldap_attr_display_name: str = "displayName"
    # 部门字段：可填多个，逗号分隔，按顺序取第一个非空值（如 "department,company,division"）
    ldap_attr_department: str = "department"
    # 上述属性都为空时，是否从 DN 的 OU 段解析部门（取最靠近 CN 的那个 OU）
    ldap_department_from_dn: bool = True
    # DN 回退时跳过的 OU（逗号分隔）：如解析到的是集团名这类粗粒度组织，可填在此处
    ldap_department_dn_exclude: str = ""
    ldap_timeout_seconds: int = 8
    # 域账号首次落库时的默认角色
    ldap_default_role: str = "member"
    # LDAP 未命中或不可用时是否回退本地口令（关=强制只走域认证）
    ldap_local_fallback: bool = True

    # ---- Redis（可选底座：缓存 / 分布式锁）----
    # redis_host 留空 = 完全禁用，所有涉及点回退现状行为（无缓存 / PG advisory 锁）。
    redis_host: str = ""
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: str = ""
    # 阿里云 APIG 读端点同步节流 / 用量缓存 TTL（秒）
    aigw_cache_ttl_seconds: int = 30
    # connector 进程锁（Redis 模式）的 TTL 与续期间隔（秒）；
    # 持锁进程崩溃后锁在 TTL 内自动过期，替代 PG advisory lock 的僵死连接问题
    connector_lock_ttl_seconds: int = 30
    # 知识路由决策缓存 TTL（秒）。知识库写路径会按库精准失效，此值仅作兜底，
    # 因此默认给 12 小时以拉高命中率；漏失效时最长 12 小时后自动新鲜。
    knowledge_route_cache_ttl_seconds: int = 12 * 3600
    # Planner 词法快速路径（2026-09-16）：当结构性上下文排除了所有路由可能
    # （无 SOP、无 pending 任务、非团队/定时、无待补槽）时，直接合成
    # answer_only 计划，跳过 planner LLM（实测省 12-15s 固定开销）。
    # 出问题置 False 一键回滚，无需改代码。
    planner_fast_path_enabled: bool = True

    # ---- 定时任务调度后端（PG 主源 + Redis 仅执行触发）----
    # "rq" = 独立 rq worker 进程经 Redis 调度（默认）；"poll" = 进程内轮询线程（旧实现）。
    # 切回旧实现只需把此值改为 "poll" 并重启，业务代码零改动。
    scheduler_backend: str = "rq"
    scheduled_task_queue: str = "scheduled_tasks"
    scheduled_task_job_timeout_seconds: int = 600
    # SOP 边条件离线编译队列。跑在同一个 rq worker 进程上（见 rq_worker），
    # 但用独立队列名，方便单独扩容 / 观察积压；Redis 不可用时自动降级为
    # 进程内异步队列（app.async_jobs），编译永远不会阻塞用户请求。
    skill_compile_queue: str = "skill_compile"
    skill_compile_job_timeout_seconds: int = 600
    # 知识库文档入库队列。入库是分钟级长任务（解析 + 两次 LLM + 切片落库），
    # 必须离开 web 进程：否则 4 线程的进程内 AsyncJob 池会被长任务占满，其它
    # 后台工作要排队到分钟级才轮到，进程重启还会直接切断正在跑的任务。
    # 跑在同一个 rq worker 进程上（见 rq_worker），独立队列名便于观察积压；
    # 想让入库不占用定时任务的消费能力，多起一个 `uv run rq-worker` 即可。
    knowledge_ingest_queue: str = "knowledge_ingest"
    # 单次入库的 rq job 超时。LLM 两阶段已收紧到最坏 330s（见 output_policy），
    # 但 PDF/DOCX 解析与切片在大文档上仍可能到十分钟级，故给 30 分钟上限——
    # 上限必须明显大于正常耗时，否则 rq worker 会 kill 掉一个其实仍在正常
    # 推进的任务，把「跑得慢」变成「跑失败」。
    knowledge_ingest_job_timeout_seconds: int = 1800
    # 聊天 PDF 附件云端解析队列（MinerU）。50 页扫描件实测 ~46s，网络抖动留
    # 足余量；超时上限同样必须明显大于正常耗时（轮询兜底 10 分钟 + 上传下载）。
    attachment_parse_queue: str = "attachment_parse"
    attachment_parse_job_timeout_seconds: int = 900
    # MinerU 云端单文件解析的轮询上限（秒）；超时视为失败，前端给出可重试提示。
    attachment_parse_poll_timeout_seconds: int = 600

    # ---- AI Reviewer（代码评审）----
    # ocr CLI（alibaba/open-code-review）在 rq worker 上跑：克隆仓库 + 调 LLM
    # 评审是分钟级任务，独立队列名便于观察积压；想不占用定时任务消费能力，
    # 多起一个 `uv run rq-worker` 即可。OCR 的 LLM 凭证经 OCR_LLM_* 环境变量
    # 注入（见 app/ai_review/runner.py），不落盘到 ocr 配置文件。
    ai_review_queue: str = "ai_review"
    ai_review_job_timeout_seconds: int = 1800
    # ocr 可执行文件：默认查 PATH；npm 装在非 PATH 目录时给绝对路径。
    ai_review_ocr_binary: str = "ocr"
    # 克隆 / fetch 与单次评审的子进程超时（秒）；评审上限须小于 rq job 超时。
    ai_review_clone_timeout_seconds: int = 600
    ai_review_review_timeout_seconds: int = 1500
    # 注入 ocr 的 LLM 端点（完整 OCR_LLM_* 配置）。留空 → 任务创建时直接失败。
    ai_review_llm_url: str = ""
    ai_review_llm_token: str = ""
    ai_review_llm_model: str = ""
    # 端点是 Anthropic 兼容（Claude）时置 true（对应 OCR_LLM_USE_ANTHROPIC）。
    ai_review_llm_use_anthropic: str = "false"
    # 评审意见语言（ocr config set language）
    ai_review_language: str = "中文"

    # MinerU 开放平台 Bearer Token（https://mineru.net）。注意历史上 .env 曾误拼
    # 为 MINUERU_TOKEN；mineru_client 会同时兼容两个键名，新环境请统一 MINERU_TOKEN。
    mineru_token: str = ""
    mineru_api_base: str = "https://mineru.net"

    model_config = SettingsConfigDict(
        env_file=_os.environ.get("ULTRARAG_DOTENV", ".env"),
        env_file_encoding="utf-8", extra="ignore",
    )

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def normalized_tool_base_url(self) -> str:
        return self.tool_base_url.rstrip("/")

    @property
    def general_skill_runtime_package_list(self) -> list[str]:
        return [item.strip() for item in self.general_skill_runtime_packages.split(",") if item.strip()]

    @property
    def model_preset_list(self) -> list[str]:
        """解析 MODEL_PRESETS 为模型名列表，忽略空项与非法 JSON。"""
        try:
            items = json.loads(self.model_presets or "[]")
        except (ValueError, TypeError):
            return []
        return [str(item).strip() for item in items if str(item).strip()]

    @property
    def ldap_base_dn_list(self) -> list[str]:
        """LDAP_BASE_DNS 按 "|" 切分为检索根列表，忽略空项。"""
        return [item.strip() for item in (self.ldap_base_dns or "").split("|") if item.strip()]

    @property
    def ldap_department_attr_list(self) -> list[str]:
        """LDAP_ATTR_DEPARTMENT 按 "," 切分为部门候选属性（按顺序取第一个非空值）。"""
        return [item.strip() for item in (self.ldap_attr_department or "").split(",") if item.strip()]

    @property
    def ldap_department_exclude_list(self) -> list[str]:
        """DN 回退解析部门时要跳过的 OU（忽略大小写比较）。"""
        return [item.strip().lower() for item in (self.ldap_department_dn_exclude or "").split(",") if item.strip()]

    @property
    def consumer_group_owner_list(self) -> list[str]:
        """解析 CONSUMER_GROUP_OWNERS 为归属列表，忽略空项与非法 JSON。"""
        try:
            items = json.loads(self.consumer_group_owners or "[]")
        except (ValueError, TypeError):
            return []
        return [str(item).strip() for item in items if str(item).strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
