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
    model_thinking_mode: str = ""
    model_thinking_models: str = ""
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
    channel_secret: str = ""
    staffdeck_role: str = "all"
    wechat_ilink_base_url: str = "https://ilinkai.weixin.qq.com"
    channel_delivery_poll_seconds: float = 1.0
    channel_delivery_max_attempts: int = 8
    public_api_enabled: bool = True
    public_api_key_pepper: str = ""
    public_api_idempotency_ttl_seconds: int = 60 * 60 * 24
    public_api_retention_days: int = 30
    public_api_webhook_timeout_seconds: float = 10.0
    public_api_webhook_max_attempts: int = 6
    # 钉钉 emotion 接口的表情常量与所需权限尚未真机验证，验证通过前默认关闭：
    # 否则常量失效或权限未开时，每条入站消息都会留下一条失败的 reaction 投递。
    channel_dingtalk_reaction_enabled: bool = False
    # 出站富文本渲染开关：开启时飞书走 post 富文本、钉钉走 markdown 消息；
    # 关闭时两者回退为纯 text 消息，用于快速回退。
    channel_rich_render_enabled: bool = True
    # 飞书渠道实时执行步骤卡片开关：开启后飞书对话在执行过程中创建并实时更新
    # 一张独立卡片展示智能体每一步（SOP/工具/知识检索），与正文回复互不影响。
    # 仅影响飞书渠道；关闭时退化为仅发最终回复。
    channel_feishu_trace_enabled: bool = True
    # 飞书 trace 卡片 SOP 紧凑展示开关：开启后匹配 SOP（判断意图/进入流程）之后的
    # 中间步骤不再逐行展示，仅显示"翻书动画 + 正在推进SOP"，等待用户补充信息时
    # 定格为"📖 流程已暂停"，SOP 结束时定格为"✅ 流程已结束"。设为 False 可整体
    # 回滚为逐行展示的旧样式；binding 的 config_json.compact_trace=false 可对单个
    # 绑定回滚。
    channel_feishu_trace_compact_sop: bool = True
    # DSH (DeepSeek Harness) engine switch. Off keeps the in-process Harness v2
    # loop untouched. On routes step execution to a DSH worker via the
    # staffdeck_dsh parallel package; dsh_staff_allowlist limits the rollout to
    # specific agent ids (comma separated) for canary testing.
    dsh_enabled: bool = False
    # Exposes /api/enterprise/dsh (module registry, snapshot preview, ledger
    # reconciliation, per-staff engine) even when turns still run on legacy, so
    # operators can inspect the pluggable tree before switching engines.
    dsh_admin_api_enabled: bool = True
    # Comma-separated module ids to disable, and extra "pkg.mod:register" specs.
    dsh_disabled_modules: str = ""
    dsh_modules: str = ""
    # Admin-editable overrides (engine / security profile / disabled / extra modules);
    # defaults to <dsh_home or cwd>/staffdeck-runtime.json.
    dsh_runtime_config_path: str = ""
    dsh_root: str = ""
    dsh_home: str = ""
    dsh_node_bin: str = "node"
    dsh_permission_mode: str = "danger-full-access"
    dsh_staff_allowlist: str = ""
    dsh_fallback_to_legacy: bool = True
    dsh_initialize_timeout_seconds: float = 90.0
    dsh_request_timeout_seconds: float = 600.0
    # OSS_LOCAL or BUSINESS_BASE; one per deployment.
    security_profile: str = "OSS_LOCAL"
    base_authz_url: str = ""
    base_authz_decision_token: str = ""
    base_authz_control_token: str = ""
    base_authz_timeout_seconds: float = 3.0
    base_authz_pending_timeout_seconds: float = 3.0
    base_identity_internal_url: str = ""
    base_identity_runtime_client_id: str = "agent-platform-runtime"
    base_identity_runtime_client_secret: str = ""
    base_workload_identity_audience: str = "staffdeck-gateway"

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
