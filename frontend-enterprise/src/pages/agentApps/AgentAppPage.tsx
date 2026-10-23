import { Navigate, useNavigate, useParams } from 'react-router-dom';

import AppHeader from '@/components/AppHeader';
import { Button as UIButton } from '@/components/ui/button';
import { isEnterpriseAdmin, type EnterpriseAuthUser } from '@/auth';
import { findAgentCatalogEntry } from '@/lib/agentCatalog';
import AiReviewerPage from '@/pages/ai-review/AiReviewerPage';
import DecisionAssistantPage from '@/pages/decision/DecisionAssistantPage';

import DocReviewWorkspace from './DocReviewWorkspace';
import OaAssistantPage from './OaAssistantPage';
import OcrWorkspace from './OcrWorkspace';
import SlidesMakerWorkspace from './SlidesMakerWorkspace';

/** adminOnly Agent 的深链回退地址（与返回按钮一致）。 */
const AGENT_APPS_PLAZA_PATH = '/enterprise/platform/agent-apps';

export type AgentAppPageProps = {
  currentUser?: EnterpriseAuthUser;
  onLogout?: () => void;
};

/**
 * Agent 广场工作台入口（`/enterprise/agent-apps/:entryId`）。
 *
 * 一个 Agent = 清单里的一条记录 + 一个前端实现；`capability` 决定用哪个工作台组件。
 * 未知 entry / 未知 capability 都退化成同一张「无法打开」卡片，而不是白屏 ——
 * 清单是手写的 JSON，写错一个字段不应该把整个路由打挂。
 */
export default function AgentAppPage({ currentUser, onLogout }: AgentAppPageProps) {
  const { entryId } = useParams<{ entryId: string }>();
  const navigate = useNavigate();
  const entry = findAgentCatalogEntry(entryId);

  // adminOnly 条目（如 AI CodeReviewer）在这里再挡一层：广场列表看不见了，
  // 深链 / 收藏链接也不能进 —— 宁可跳回广场也不能给成员露出管理员工具。
  if (entry?.adminOnly && !isEnterpriseAdmin(currentUser)) {
    return <Navigate to={AGENT_APPS_PLAZA_PATH} replace />;
  }

  if (entry && entry.capability === 'slides') {
    return <SlidesMakerWorkspace entry={entry} currentUser={currentUser} onLogout={onLogout} />;
  }

  // AI 文档审阅：上传 docx → 一键审阅 / 对话修改 → 导出保留原格式的 Word。
  if (entry && entry.capability === 'doc-review') {
    return <DocReviewWorkspace entry={entry} currentUser={currentUser} onLogout={onLogout} />;
  }

  // OCR 助手：PDF / 图片 → Markdown / 纯文本 / 带行坐标的结构化结果。不走模型。
  if (entry && entry.capability === 'ocr') {
    return <OcrWorkspace entry={entry} currentUser={currentUser} onLogout={onLogout} />;
  }

  // 决策助手：原独立页面整体搬进 Agent 广场，渲染外壳已与 PPT Studio 统一（页头 + 返回按钮 + 定高工作卡）。
  if (entry && entry.capability === 'decision') {
    return <DecisionAssistantPage currentUser={currentUser} onLogout={onLogout} />;
  }

  // OA 浏览器代填：书签把面板注入用户当前的 OA 页面，服务端不开浏览器，用户零安装。
  if (entry && entry.capability === 'oa-assistant') {
    return <OaAssistantPage currentUser={currentUser} onLogout={onLogout} />;
  }

  // AI CodeReviewer：原独立页面整体搬进 Agent 广场，外壳同样统一；仅管理员可见（上面已挡）。
  if (entry && entry.capability === 'code-review') {
    return <AiReviewerPage currentUser={currentUser} onLogout={onLogout} />;
  }

  return (
    <div className="min-h-full box-border px-[48px] pt-[20px] pb-[43px] max-[900px]:px-[16px]">
      <AppHeader className="mb-[16px]" onLogout={onLogout} userName={currentUser?.username} title="Agent 广场" />
      <div className="grid min-h-[320px] place-items-center content-center gap-[12px] rounded-[20px] bg-white px-[20px] py-[48px] text-center shadow-[0_-4px_16px_0_rgba(0,0,0,0.05)]">
        <span className="text-[15px] font-medium text-[#18181a]">该 Agent 暂时无法打开</span>
        <span className="text-[12px] leading-[1.7] text-[#858b9c]">
          {entry
            ? `Agent「${entry.name}」声明了未知的能力「${entry.capability || '空'}」，请联系平台维护者确认清单配置。`
            : '清单里没有这个 Agent，可能已被移除。'}
        </span>
        <UIButton
          onClick={() => navigate(AGENT_APPS_PLAZA_PATH)}
          className="h-8 rounded-[10px] bg-[#18181a] px-5 text-[12px] font-normal text-white hover:bg-[#303030]"
        >
          返回 Agent 广场
        </UIButton>
      </div>
    </div>
  );
}
