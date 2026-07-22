import { useCallback, useEffect, useMemo, useState, type CSSProperties } from "react";
import {
  BrowserRouter,
  Navigate,
  Route,
  Routes,
  useLocation,
  useNavigate,
} from "react-router-dom";
import { api, isAuthError, TENANT_ID } from "./api/client";
import {
  clearEnterpriseAuthSession,
  getEnterpriseAuthSession,
  isEnterpriseAdmin,
  isGalleryEmployee,
  setEnterpriseAuthSession,
  type EnterpriseAuthSession,
  type EnterpriseAuthUser,
} from "./auth";
import AppSidebar from "./components/AppSidebar";
import OnboardingGuide, { ONBOARDING_SEEN_KEY } from "./components/OnboardingGuide";
import QuickStartGuide, {
  QUICK_START_COMPLETED_EVENT,
  QUICK_START_SEEN_KEY,
} from "./components/QuickStartGuide";
import StaffdeckIcon from "./components/StaffdeckIcon";
import { SidebarProvider } from "@/components/ui/sidebar";
import { EnterpriseRoute } from "./enums/routes";
import {
  employeeBlankMetadata,
  canAccessEmployeeAgent,
  canManageEmployeeAgent,
  canSelectCurrentEmployeeAgent,
  employeeDisplayName,
  employeeDisplayNameWithCreator,
  employeeProfile,
  preferredEmployeeAgent,
} from "./employee";
import AccountsPage from "./pages/AccountsPage";
import AgentsPage from "./pages/AgentsPage";
import ChatPage from "./pages/chat/ChatPage";
import ChatGalleryPage from "./pages/chat/ChatGalleryPage";
import DashboardPage from "./pages/dashboard/DashboardPage";
import EmptyEmployeeState from "./components/EmptyEmployeeState";
import DistillPage from "./pages/DistillPage";
import GeneralSkillsPage, {
  GeneralSkillEditPage,
  GeneralSkillNewPage,
} from "./pages/GeneralSkillsPage";
import KnowledgeManagePage, { KnowledgeAddPage } from "./pages/KnowledgePage";
import LoginPage from "./pages/LoginPage";
import ModelsPage from "./pages/ModelsPage";
import OpenPlatformPage from "./pages/OpenPlatformPage";
import SkillsPage from "./pages/SkillsPage";
import {
  ScheduledTaskEditPage,
  ScheduledTaskNewPage,
} from "./pages/dashboard/ScheduledTasksTab";
import ToolsPage, {
  McpServerEditPage,
  McpServerNewPage,
  ToolEditPage,
  ToolNewPage,
  ToolTestPage,
} from "./pages/ToolsPage";
import { useIsMobile } from "./hooks/use-mobile";
import {
  Dialog,
  DialogContent,
  DialogTitle,
  Input,
  Select as UISelect,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Textarea,
} from "@/components/ui";
import { Button as UIButton } from "@/components/ui/button";
import { Toaster } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { notify } from "@/components/ui/app-toast";
import {
  emitAgentScopeChange,
  ENTERPRISE_AGENT_STORAGE_KEY,
  persistSharedAgentScope,
} from "@/lib/agent-scope-storage";
import { cn } from "@/lib/utils";
import {
  SELECT_TRIGGER_CLASS,
  DIALOG_CANCEL_BUTTON_CLASS,
  DIALOG_FOOTER_CLASS,
  DIALOG_PRIMARY_BUTTON_CLASS,
} from "@/lib/enterprise-ui";
import type { AgentProfileRead, ModelConfigRead } from "./types";
import { useI18n } from "./i18n";

const ENTERPRISE_SIDEBAR_STORAGE_KEY = "ultrarag_enterprise_sidebar_expanded";
const MODEL_CONFIGS_UPDATED_EVENT = "ultrarag-enterprise-model-configs-updated";
type AgentCreateMode = "copy" | "blank";

type AgentCreateFormState = {
  name: string;
  description: string;
  roleName: string;
  sourceMode: AgentCreateMode;
  copyFromAgentId: string;
};

const EMPTY_AGENT_FORM: AgentCreateFormState = {
  name: "",
  description: "",
  roleName: "",
  sourceMode: "copy",
  copyFromAgentId: "",
};

function Shell({
  auth,
  onLogout,
}: {
  auth: EnterpriseAuthSession;
  onLogout: () => void;
}) {
  const navigate = useNavigate();
  const location = useLocation();
  const { t } = useI18n();
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  const [agentsLoaded, setAgentsLoaded] = useState(false);
  const [selectedAgentId, setSelectedAgentId] = useState(
    () => window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) || "",
  );
  const [sidebarExpanded, setSidebarExpanded] = useState(() => {
    const stored = window.localStorage.getItem(ENTERPRISE_SIDEBAR_STORAGE_KEY);
    return stored == null ? true : stored === "1";
  });
  const [agentCreateOpen, setAgentCreateOpen] = useState(false);
  const [agentForm, setAgentForm] =
    useState<AgentCreateFormState>(EMPTY_AGENT_FORM);
  const [modelConfigs, setModelConfigs] = useState<ModelConfigRead[]>([]);
  const [modelConfigsLoaded, setModelConfigsLoaded] = useState(false);
  const [guidesCompleted, setGuidesCompleted] = useState(() => Boolean(
    window.localStorage.getItem(ONBOARDING_SEEN_KEY)
    && window.localStorage.getItem(QUICK_START_SEEN_KEY),
  ));
  const isMobile = useIsMobile();
  const isAdmin = isEnterpriseAdmin(auth.user);
  const accountRoleLabel = isAdmin ? "管理员" : "";
  const isDistillRoute = location.pathname === "/enterprise/skills/distill";
  const selected =
    location.pathname === "/enterprise"
      ? "/enterprise/dashboard"
      : location.pathname.startsWith("/enterprise/platform")
        ? "/enterprise/platform"
        : location.pathname.startsWith("/enterprise/knowledge")
          ? "/enterprise/knowledge"
          : location.pathname.startsWith("/enterprise/general-skills")
            ? "/enterprise/general-skills"
            : location.pathname.startsWith("/enterprise/tools")
              ? "/enterprise/tools"
              : location.pathname.startsWith("/enterprise/scheduled-tasks")
                ? "/enterprise/scheduled-tasks"
                : isDistillRoute
                  ? "/enterprise/skills"
                  : location.pathname;
  const isAgentRosterRoute = location.pathname.startsWith("/enterprise/agents");
  const [lastDistillSearch, setLastDistillSearch] = useState(() =>
    isDistillRoute ? location.search : "",
  );
  const distillSearch = isDistillRoute ? location.search : lastDistillSearch;
  const distillSearchParams = useMemo(
    () => new URLSearchParams(distillSearch),
    [distillSearch],
  );

  useEffect(() => {
    if (isDistillRoute) {
      setLastDistillSearch(location.search);
    }
  }, [isDistillRoute, location.search]);

  useEffect(() => {
    loadAgents();
  }, []);

  const loadModelConfigs = useCallback(() => {
    return api
      .get<ModelConfigRead[]>(`/api/enterprise/model-configs?tenant_id=${TENANT_ID}`)
      .then((items) => {
        setModelConfigs(items);
        setModelConfigsLoaded(true);
      })
      .catch(() => {
        setModelConfigs([]);
        setModelConfigsLoaded(false);
      });
  }, []);

  useEffect(() => {
    void loadModelConfigs();
  }, [loadModelConfigs]);

  useEffect(() => {
    const onModelConfigsUpdated = (event: Event) => {
      const rows = (event as CustomEvent<{ models?: ModelConfigRead[] }>).detail?.models;
      if (rows) {
        setModelConfigs(rows);
        setModelConfigsLoaded(true);
      } else {
        void loadModelConfigs();
      }
    };
    window.addEventListener(MODEL_CONFIGS_UPDATED_EVENT, onModelConfigsUpdated);
    return () => window.removeEventListener(MODEL_CONFIGS_UPDATED_EVENT, onModelConfigsUpdated);
  }, [loadModelConfigs]);

  useEffect(() => {
    const onQuickStartCompleted = () => setGuidesCompleted(true);
    window.addEventListener(QUICK_START_COMPLETED_EVENT, onQuickStartCompleted);
    return () => window.removeEventListener(QUICK_START_COMPLETED_EVENT, onQuickStartCompleted);
  }, []);

  // Auto-collapse the sidebar on small screens; restore the saved preference on desktop.
  useEffect(() => {
    if (isMobile) {
      setSidebarExpanded(false);
    } else {
      const stored = window.localStorage.getItem(
        ENTERPRISE_SIDEBAR_STORAGE_KEY,
      );
      setSidebarExpanded(stored == null ? true : stored === "1");
    }
  }, [isMobile]);

  useEffect(() => {
    const onAgentRefresh = () => {
      void loadAgents();
    };
    window.addEventListener(
      "ultrarag-enterprise-agent-scope-refresh",
      onAgentRefresh,
    );
    return () =>
      window.removeEventListener(
        "ultrarag-enterprise-agent-scope-refresh",
        onAgentRefresh,
      );
  }, []);

  useEffect(() => {
    const onScopeChange = (event: Event) => {
      const nextAgentId =
        (event as CustomEvent<{ agentId?: string }>).detail?.agentId ||
        window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) ||
        "";
      if (nextAgentId) {
        persistSharedAgentScope(nextAgentId, auth.user.id);
        const knownSelectableAgent = agents.some(
          (item) => item.id === nextAgentId && canUseAgentScope(item),
        );
        if (!knownSelectableAgent) void loadAgents(nextAgentId);
      }
      setSelectedAgentId(nextAgentId);
    };
    window.addEventListener(
      "ultrarag-enterprise-agent-scope-change",
      onScopeChange,
    );
    return () =>
      window.removeEventListener(
        "ultrarag-enterprise-agent-scope-change",
        onScopeChange,
      );
  }, [agents, auth.user.id]);

  useEffect(() => {
    const onCreateAgent = () => openCreateAgentModal();
    window.addEventListener("ultrarag-enterprise-agent-create", onCreateAgent);
    return () =>
      window.removeEventListener(
        "ultrarag-enterprise-agent-create",
        onCreateAgent,
      );
  }, []);

  function loadAgents(preferredAgentId = "") {
    return api
      .get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${TENANT_ID}`)
      .then((rows) => {
        setAgents(rows);
        const selectableRows = rows.filter((item) => canUseAgentScope(item));
        setSelectedAgentId((current) => {
          const requestedAgentId = preferredAgentId || current;
          if (
            requestedAgentId &&
            selectableRows.some((item) => item.id === requestedAgentId)
          ) {
            persistSharedAgentScope(requestedAgentId, auth.user.id);
            return requestedAgentId;
          }
          const manageableRows = selectableRows.filter((item) =>
            canManageEmployeeAgent(item, auth.user),
          );
          const next = isAdmin
            ? preferredEmployeeAgent(selectableRows)?.id || ""
            : preferredEmployeeAgent(manageableRows)?.id ||
              preferredEmployeeAgent(selectableRows)?.id ||
              "";
          if (next) {
            persistSharedAgentScope(next, auth.user.id);
            if (next !== current) {
              emitAgentScopeChange(next);
            }
          }
          return next;
        });
      })
      .catch(() => setAgents([]))
      .finally(() => setAgentsLoaded(true));
  }

  function canUseAgentScope(agent: AgentProfileRead): boolean {
    return canSelectCurrentEmployeeAgent(agent, auth.user, { activeOnly: true });
  }

  function changeAgentScope(agentId: string) {
    setSelectedAgentId(agentId);
    persistSharedAgentScope(agentId, auth.user.id);
    emitAgentScopeChange(agentId);
  }

  function handleSidebarOpenChange(open: boolean) {
    setSidebarExpanded(open);
    window.localStorage.setItem(
      ENTERPRISE_SIDEBAR_STORAGE_KEY,
      open ? "1" : "0",
    );
  }

  const scopeAgents = agents.filter(canUseAgentScope);
  const hasUsableModelConfig = modelConfigs.some((item) => item.enabled);
  const showModelSetupNotice = guidesCompleted && modelConfigsLoaded && !hasUsableModelConfig;
  const modelSetupNoticeText = isAdmin
    ? t("还没有可用模型配置，数字员工暂不能调用模型。请先完成模型配置。")
    : t("系统管理员尚未配置可用模型，数字员工暂不能调用模型。请联系管理员完成模型配置。");
  const selectedAgent = scopeAgents.find((item) => item.id === selectedAgentId);
  const sidebarAgent = selectedAgent;
  // Routes that operate on a specific employee; show the empty guide when none exist.
  const EMPLOYEE_SCOPED_PREFIXES = [
    "/enterprise/dashboard",
    "/enterprise/scheduled-tasks",
    "/enterprise/memories",
    "/enterprise/feedback",
    "/enterprise/knowledge",
    "/enterprise/general-skills",
    "/enterprise/skills",
    "/enterprise/tools",
  ];
  const hasEmployees = scopeAgents.some((item) => !item.is_overall);
  const isEmployeeScopedRoute = EMPLOYEE_SCOPED_PREFIXES.some((prefix) =>
    location.pathname.startsWith(prefix),
  );
  const showEmployeeEmptyState =
    agentsLoaded && !hasEmployees && isEmployeeScopedRoute;
  const sourceAgents = agents.filter((item) =>
    canAccessEmployeeAgent(item, auth.user, {
      activeOnly: true,
      includeOverall: isAdmin,
    }),
  );
  const selectedAgentName = selectedAgent
    ? employeeDisplayName(selectedAgent)
    : "未选择";
  const selectedAgentCaption = selectedAgent
    ? selectedAgent.is_overall
      ? "开放广场"
      : employeeProfile(selectedAgent).roleName
    : "-";
  function openCreateAgentModal() {
    setAgentForm({
      ...EMPTY_AGENT_FORM,
      copyFromAgentId: selectedAgentId || sourceAgents[0]?.id || "",
    });
    setAgentCreateOpen(true);
  }

  async function saveAgentCreateModal() {
    const name = agentForm.name.trim();
    if (!name) {
      notify.error("请填写数字员工姓名");
      return;
    }
    const isBlankOnboarding = agentForm.sourceMode === "blank";
    const sourceAgent = agentForm.copyFromAgentId
      ? sourceAgents.find((item) => item.id === agentForm.copyFromAgentId)
      : undefined;
    const sourceMetadata =
      !isBlankOnboarding && sourceAgent?.metadata ? sourceAgent.metadata : {};
    const sourceRoleName =
      sourceAgent && !sourceAgent.is_overall
        ? employeeProfile(sourceAgent).roleName
        : "";
    const roleName =
      agentForm.roleName.trim() ||
      (!isBlankOnboarding ? sourceRoleName : "") ||
      "待补充职位";
    const description =
      agentForm.description.trim() ||
      (!isBlankOnboarding
        ? sourceAgent?.description ||
          String(sourceMetadata.system_prompt_summary || "")
        : "") ||
      "";
    const baseMetadata = {
      ...sourceMetadata,
      system_prompt_summary: description,
      owner_user_id: auth.user.id,
      owner_username: auth.user.username,
      owner_display_name: auth.user.display_name || auth.user.username,
      created_by_user_id: auth.user.id,
      created_by_username: auth.user.username,
      created_by: auth.user.username,
      created_by_display_name: auth.user.display_name || auth.user.username,
      creator_name: auth.user.username,
      role_key: "",
      role_name: roleName,
      onboarded_at: new Date().toISOString().slice(0, 10),
      blank_onboarding: isBlankOnboarding,
    };
    try {
      const created = await api.post<AgentProfileRead>(
        "/api/enterprise/agents",
        {
          tenant_id: TENANT_ID,
          name,
          description,
          source_mode: agentForm.sourceMode,
          copy_from_agent_id:
            agentForm.sourceMode === "copy"
              ? agentForm.copyFromAgentId || undefined
              : undefined,
          metadata: isBlankOnboarding
            ? employeeBlankMetadata(baseMetadata)
            : baseMetadata,
        },
      );
      await loadAgents();
      changeAgentScope(created.id);
      setAgentCreateOpen(false);
      notify.success("数字员工创建成功");
    } catch (error) {
      notify.error(error instanceof Error ? error.message : "创建数字员工失败");
    }
  }

  return (
    <SidebarProvider
      open={sidebarExpanded}
      onOpenChange={handleSidebarOpenChange}
      style={
        {
          "--sidebar-width": "220px",
          "--sidebar-width-icon": "72px",
        } as CSSProperties
      }
      className={`app-shell ${sidebarExpanded ? "sidebar-expanded" : "sidebar-collapsed"} ${isAgentRosterRoute ? "is-agent-roster" : ""}`}
    >
      <AppSidebar
        selected={selected}
        onNavigate={navigate}
        isAdmin={isAdmin}
        sidebarAgent={sidebarAgent}
        scopeAgents={scopeAgents}
        selectedAgentId={selectedAgentId}
        onSelectAgent={(agentId) => {
          if (agentId !== selectedAgentId) changeAgentScope(agentId);
          navigate(EnterpriseRoute.Dashboard);
        }}
        onOpenChat={() => {
          navigate(EnterpriseRoute.Gallery);
        }}
        modelSetupAttention={isAdmin && showModelSetupNotice}
      />
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <div
          className={`content flex-1 ${isDistillRoute ? "flex min-h-0 flex-col overflow-hidden p-0!" : ""} ${selected === "/enterprise/dashboard" ? "sd1-dashboard-content" : ""} ${selected !== "/enterprise/dashboard" && !isDistillRoute ? "sd1-management-content" : ""}`}
        >
          {showModelSetupNotice && (
            <div className="mx-[24px] mt-[18px] mb-[10px] flex shrink-0 flex-col items-start justify-between gap-[12px] rounded-[12px] border border-[#f3d28b] bg-[#fff8e8] px-[18px] py-[12px] text-[#6f4500] shadow-[0_8px_24px_rgba(92,62,0,0.08)] sm:flex-row sm:items-center">
              <div className="flex min-w-0 items-center gap-[10px]">
                <span className="flex size-[28px] shrink-0 items-center justify-center rounded-[8px] bg-[#ffe7ad] text-[#8a4b00]">
                  <StaffdeckIcon name="model" className="size-[15px]" />
                </span>
                <span className="min-w-0 text-[13px] leading-[20px]">{modelSetupNoticeText}</span>
              </div>
              {isAdmin && (
                <UIButton
                  type="button"
                  size="sm"
                  onClick={() => navigate(EnterpriseRoute.Models)}
                  className="h-[32px] shrink-0 rounded-[8px] bg-[#1a71ff] px-[12px] text-[12px] text-white hover:bg-[#0f5ed7]"
                >
                  {t("去配置")}
                </UIButton>
              )}
            </div>
          )}
          <div
            className={
              isDistillRoute
                ? "persistent-distill active flex min-h-0 flex-1 flex-col"
                : "persistent-distill hidden"
            }
          >
            <DistillPage
              active={isDistillRoute}
              searchParamsOverride={distillSearchParams}
              currentUser={auth.user}
              onLogout={onLogout}
            />
          </div>
          {!isDistillRoute && showEmployeeEmptyState && (
            <EmptyEmployeeState
              isAdmin={isAdmin}
              onCreate={openCreateAgentModal}
              onBrowsePlatform={() => navigate(EnterpriseRoute.Platform)}
            />
          )}
          {!isDistillRoute && !showEmployeeEmptyState && (
            <Routes>
              <Route
                path="/enterprise"
                element={<Navigate to="/enterprise/dashboard" replace />}
              />
              <Route
                path="/enterprise/platform"
                element={
                  <OpenPlatformPage
                    currentUser={auth.user}
                    isAdmin={isAdmin}
                    onLogout={onLogout}
                  />
                }
              />
              <Route
                path="/enterprise/platform/:kind"
                element={
                  <OpenPlatformPage
                    currentUser={auth.user}
                    isAdmin={isAdmin}
                    onLogout={onLogout}
                  />
                }
              />
              <Route
                path="/enterprise/dashboard"
                element={
                  <DashboardPage
                    currentUser={auth.user}
                    isAdmin={isAdmin}
                    onLogout={onLogout}
                  />
                }
              />
              <Route
                path="/enterprise/agents"
                element={
                  <AgentsPage
                    currentUser={auth.user}
                    isAdmin={isAdmin}
                    onCreateAgent={openCreateAgentModal}
                    onLogout={onLogout}
                  />
                }
              />
              <Route
                path="/enterprise/memories"
                element={
                  <DashboardPage
                    currentUser={auth.user}
                    isAdmin={isAdmin}
                    profileTab="memories"
                    onLogout={onLogout}
                  />
                }
              />
              <Route
                path="/enterprise/knowledge"
                element={
                  <KnowledgeManagePage
                    currentUser={auth.user}
                    onLogout={onLogout}
                  />
                }
              />
              <Route
                path="/enterprise/knowledge/new"
                element={
                  <KnowledgeAddPage
                    currentUser={auth.user}
                    onLogout={onLogout}
                  />
                }
              />
              <Route
                path="/enterprise/feedback"
                element={
                  <DashboardPage
                    currentUser={auth.user}
                    isAdmin={isAdmin}
                    profileTab="logs"
                    onLogout={onLogout}
                  />
                }
              />
              <Route
                path="/enterprise/scheduled-tasks"
                element={
                  <DashboardPage
                    currentUser={auth.user}
                    isAdmin={isAdmin}
                    profileTab="scheduled"
                    onLogout={onLogout}
                  />
                }
              />
              <Route
                path="/enterprise/scheduled-tasks/new"
                element={
                  <ScheduledTaskNewPage
                    currentUser={auth.user}
                    onLogout={onLogout}
                  />
                }
              />
              <Route
                path="/enterprise/scheduled-tasks/:taskId/edit"
                element={
                  <ScheduledTaskEditPage
                    currentUser={auth.user}
                    onLogout={onLogout}
                  />
                }
              />
              <Route
                path="/enterprise/skills"
                element={
                  <SkillsPage currentUser={auth.user} onLogout={onLogout} />
                }
              />
              <Route
                path="/enterprise/general-skills"
                element={
                  <GeneralSkillsPage
                    currentUser={auth.user}
                    onLogout={onLogout}
                  />
                }
              />
              <Route
                path="/enterprise/general-skills/new"
                element={
                  <GeneralSkillNewPage
                    currentUser={auth.user}
                    onLogout={onLogout}
                  />
                }
              />
              <Route
                path="/enterprise/general-skills/:slug/edit"
                element={
                  <GeneralSkillEditPage
                    currentUser={auth.user}
                    onLogout={onLogout}
                  />
                }
              />
              <Route
                path="/enterprise/accounts"
                element={
                  isAdmin ? (
                    <AccountsPage currentUser={auth.user} onLogout={onLogout} />
                  ) : (
                    <Navigate to={EnterpriseRoute.Gallery} replace />
                  )
                }
              />
              <Route
                path="/enterprise/models"
                element={
                  isAdmin ? (
                    <ModelsPage currentUser={auth.user} onLogout={onLogout} />
                  ) : (
                    <Navigate to={EnterpriseRoute.Gallery} replace />
                  )
                }
              />
              <Route
                path="/enterprise/tools"
                element={
                  <ToolsPage currentUser={auth.user} onLogout={onLogout} />
                }
              />
              <Route
                path="/enterprise/tools/new"
                element={
                  <ToolNewPage currentUser={auth.user} onLogout={onLogout} />
                }
              />
              <Route
                path="/enterprise/tools/mcp/new"
                element={
                  <McpServerNewPage
                    currentUser={auth.user}
                    onLogout={onLogout}
                  />
                }
              />
              <Route
                path="/enterprise/tools/mcp/:serverId/edit"
                element={
                  <McpServerEditPage
                    currentUser={auth.user}
                    onLogout={onLogout}
                  />
                }
              />
              <Route
                path="/enterprise/tools/:toolId/edit"
                element={
                  <ToolEditPage currentUser={auth.user} onLogout={onLogout} />
                }
              />
              <Route
                path="/enterprise/tools/:toolId/test"
                element={
                  <ToolTestPage currentUser={auth.user} onLogout={onLogout} />
                }
              />
              <Route
                path="/enterprise/persona"
                element={<Navigate to="/enterprise/dashboard" replace />}
              />
              <Route
                path="*"
                element={<Navigate to="/enterprise/dashboard" replace />}
              />
            </Routes>
          )}
        </div>
      </div>
      <Dialog open={agentCreateOpen} onOpenChange={setAgentCreateOpen}>
        <DialogContent className="flex max-h-[calc(100dvh-32px)] w-[calc(100%-32px)] flex-col gap-0 overflow-hidden rounded-[16px] p-0 sm:max-w-[520px]">
          <DialogTitle className="shrink-0 px-[24px] py-[16px] text-[16px] font-semibold text-foreground">
            新建数字员工
          </DialogTitle>
          <div className="agent-editor-form min-h-0 flex-1 overflow-y-auto px-[24px] pb-[16px]">
            <label>
              创建方式
              <div className="inline-flex w-fit gap-[4px] rounded-[10px] border border-border p-[2px]">
                {[
                  { label: "从广场复制", value: "copy" as const },
                  { label: "从空白开始", value: "blank" as const },
                ].map((option) => (
                  <button
                    key={option.value}
                    type="button"
                    className={cn(
                      "rounded-[8px] px-[14px] py-[5px] text-[13px] font-medium transition-colors",
                      agentForm.sourceMode === option.value
                        ? "bg-[#18181a] text-white"
                        : "text-[#5b6273] hover:text-foreground",
                    )}
                    onClick={() =>
                      setAgentForm((prev) => ({
                        ...prev,
                        sourceMode: option.value,
                        copyFromAgentId:
                          option.value === "blank" ? "" : prev.copyFromAgentId,
                      }))
                    }
                  >
                    {option.label}
                  </button>
                ))}
              </div>
            </label>
            <label>
              职位
              <Input
                value={agentForm.roleName}
                onChange={(event) =>
                  setAgentForm((prev) => ({
                    ...prev,
                    roleName: event.target.value,
                  }))
                }
                placeholder="例如 研发工程师、财务助理"
              />
            </label>
            <div className="grid content-start gap-[6px]">
            {agentForm.sourceMode === "copy" && (
              <label>
                复制来源
                <UISelect
                  value={agentForm.copyFromAgentId || undefined}
                  onValueChange={(value) =>
                    setAgentForm((prev) => {
                      const nextSource = sourceAgents.find(
                        (item) => item.id === value,
                      );
                      return {
                        ...prev,
                        copyFromAgentId: value,
                        roleName:
                          prev.roleName ||
                          (nextSource && !nextSource.is_overall
                            ? employeeProfile(nextSource).roleName
                            : ""),
                      };
                    })
                  }
                >
                  <SelectTrigger className={cn(SELECT_TRIGGER_CLASS, "w-full")}>
                    <SelectValue placeholder="选择复制来源" />
                  </SelectTrigger>
                  <SelectContent>
                    {sourceAgents.map((agent) => (
                      <SelectItem key={agent.id} value={agent.id}>
                        {agent.is_overall
                          ? "开放广场"
                          : `${employeeDisplayNameWithCreator(agent)} · ${employeeProfile(agent).roleName}${isGalleryEmployee(agent) ? " · 广场" : ""}`}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </UISelect>
              </label>
            )}
            {agentForm.sourceMode === "blank" && (
              <div className="agent-definition-note">
                从空白开始创建，不继承任何已有配置。
              </div>
            )}
            </div>
            <label>
              数字员工姓名
              <Input
                value={agentForm.name}
                onChange={(event) =>
                  setAgentForm((prev) => ({
                    ...prev,
                    name: event.target.value,
                  }))
                }
              />
            </label>
            <label>
              岗位描述
              <Textarea
                rows={3}
                value={agentForm.description}
                onChange={(event) =>
                  setAgentForm((prev) => ({
                    ...prev,
                    description: event.target.value,
                  }))
                }
                placeholder="概括这个数字员工的岗位边界、服务风格和执行重点"
              />
            </label>
          </div>
          <div className={cn(DIALOG_FOOTER_CLASS, "shrink-0 border-t border-border")}>
            <UIButton
              variant="outline"
              className={DIALOG_CANCEL_BUTTON_CLASS}
              onClick={() => setAgentCreateOpen(false)}
            >
              取消
            </UIButton>
            <UIButton
              className={DIALOG_PRIMARY_BUTTON_CLASS}
              onClick={() => void saveAgentCreateModal()}
            >
              创建
            </UIButton>
          </div>
        </DialogContent>
      </Dialog>
    </SidebarProvider>
  );
}

function AuthedApp({
  auth,
  onLogout,
}: {
  auth: EnterpriseAuthSession;
  onLogout: () => void;
}) {
  const location = useLocation();
  if (location.pathname === "/") {
    return <Navigate to={EnterpriseRoute.Gallery} replace />;
  }
  if (location.pathname === "/chat" || location.pathname === "/chat/") {
    return <Navigate to={EnterpriseRoute.Gallery} replace />;
  }
  if (location.pathname.startsWith("/chat/draft/")) {
    const nextPath = location.pathname.replace(/^\/chat/, EnterpriseRoute.Chat);
    return <Navigate to={`${nextPath}${location.search}`} replace />;
  }
  if (location.pathname.startsWith("/chat/session_")) {
    const nextPath = location.pathname.replace(/^\/chat/, EnterpriseRoute.Chat);
    return <Navigate to={`${nextPath}${location.search}`} replace />;
  }
  if (location.pathname === "/enterprise/chat" || location.pathname === "/enterprise/chat/") {
    return <Navigate to={EnterpriseRoute.Gallery} replace />;
  }
  if (location.pathname.startsWith("/enterprise/chat/draft/")) {
    const nextPath = location.pathname.replace(/^\/enterprise\/chat/, EnterpriseRoute.Chat);
    return <Navigate to={`${nextPath}${location.search}`} replace />;
  }
  if (location.pathname.startsWith("/enterprise/chat/session_")) {
    const nextPath = location.pathname.replace(/^\/enterprise\/chat/, EnterpriseRoute.Chat);
    return <Navigate to={`${nextPath}${location.search}`} replace />;
  }
  if (location.pathname.startsWith(EnterpriseRoute.Workspace)) {
    return (
      <Routes>
        <Route
          path="/workspace"
          element={<Navigate to="/workspace/gallery" replace />}
        />
        <Route path="/workspace/gallery" element={<ChatGalleryPage />} />
        <Route path="/workspace/chat" element={<ChatPage />} />
        <Route
          path="/workspace/chat/draft/:draftAgentId"
          element={<ChatPage />}
        />
        <Route path="/workspace/chat/:sessionId" element={<ChatPage />} />
      </Routes>
    );
  }
  return <Shell auth={auth} onLogout={onLogout} />;
}

export default function App() {
  // Subscribe the application tree to locale changes so locale-sensitive dates
  // and computed labels update without remounting or losing form state.
  useI18n();
  const [auth, setAuth] = useState<EnterpriseAuthSession | null>(() =>
    getEnterpriseAuthSession(),
  );
  const [authChecked, setAuthChecked] = useState(() => !auth?.token);

  useEffect(() => {
    if (!auth?.token) {
      setAuthChecked(true);
      return undefined;
    }
    let cancelled = false;
    setAuthChecked(false);
    void api.get<EnterpriseAuthUser>("/api/auth/me")
      .then((user) => {
        if (cancelled) return;
        const refreshed = { token: auth.token, user };
        setEnterpriseAuthSession(refreshed);
        setAuth(refreshed);
        setAuthChecked(true);
      })
      .catch((error) => {
        if (cancelled) return;
        if (isAuthError(error)) {
          clearEnterpriseAuthSession();
          setAuth(null);
        }
        setAuthChecked(true);
      });
    return () => {
      cancelled = true;
    };
  }, [auth?.token]);

  function logout() {
    clearEnterpriseAuthSession();
    setAuth(null);
    setAuthChecked(true);
  }

  return (
    <TooltipProvider>
      <BrowserRouter>
        <Routes>
          <Route
            path="/*"
            element={
              auth && !authChecked ? null : auth ? (
                <AuthedApp auth={auth} onLogout={logout} />
              ) : (
                <LoginPage onLogin={setAuth} />
              )
            }
          />
        </Routes>
        {auth && authChecked ? <OnboardingGuide /> : null}
        {auth && authChecked ? <QuickStartGuide isAdmin={isEnterpriseAdmin(auth.user)} /> : null}
      </BrowserRouter>
      <Toaster richColors closeButton position="top-center" />
    </TooltipProvider>
  );
}
