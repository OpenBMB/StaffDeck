import { useEffect, useLayoutEffect, useMemo, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import {
  Popover,
  PopoverAnchor,
  PopoverArrow,
  PopoverContent,
  PopoverDescription,
  PopoverTitle,
} from "@/components/ui/popover";
import { Button } from "@/components/ui/button";
import { XIcon } from "lucide-react";
import { EnterpriseRoute } from "@/enums/routes";
import { OPEN_QUICK_START_EVENT } from "./OnboardingGuide";

const ONBOARDING_SEEN_KEY = "staffdeck_onboarding_guide_seen";
export const QUICK_START_SEEN_KEY = "staffdeck_quick_start_guide_seen";
export const QUICK_START_COMPLETED_EVENT = "staffdeck-quick-start-completed";
export const OPEN_MODEL_CREATE_EVENT = "staffdeck-open-model-create";

type QuickStartStep = {
  title: string;
  description: string;
  route: EnterpriseRoute;
  target: string;
  fallbackTarget?: string;
  actionTarget?: string;
  side?: "top" | "right" | "bottom" | "left";
  nextLabel?: string;
  nextRoute?: EnterpriseRoute;
  eventName?: string;
};

const STEPS: QuickStartStep[] = [
  {
    title: "配置模型 API Key",
    description: "模型是数字员工的大脑。点击「新建模型」，填写 Base URL、Model 和 API Key 即可接入。",
    route: EnterpriseRoute.Models,
    target: "models-create",
    eventName: OPEN_MODEL_CREATE_EVENT,
  },
  {
    title: "创建你的数字员工",
    description: "在这里新建数字员工，绑定模型、知识库、技能与 SOP，它就正式上岗了。",
    route: EnterpriseRoute.Agents,
    target: "route-/enterprise/agents",
    side: "right",
  },
  {
    title: "开放广场 · 共享与复用",
    description: "汇集可共享的 SOP、知识库、技能和工具，新建数字员工时可以直接复制作为起点。",
    route: EnterpriseRoute.Platform,
    target: "route-/enterprise/platform",
    side: "right",
  },
  {
    title: "员工档案 · 一目了然",
    description: "查看数字员工的基本信息、能力配置和工作情况，像翻真人员工的档案一样。",
    route: EnterpriseRoute.Dashboard,
    target: "route-/enterprise/dashboard",
    side: "right",
  },
  {
    title: "定时任务 · 主动干活",
    description: "设置周期任务，数字员工到点自动执行，比如每天早上生成日报。",
    route: EnterpriseRoute.ScheduledTasks,
    target: "route-/enterprise/scheduled-tasks",
    side: "right",
  },
  {
    title: "记忆 · 越用越懂你",
    description: "数字员工会记住对话中的关键信息，越用越了解你的业务和习惯。",
    route: EnterpriseRoute.Memories,
    target: "route-/enterprise/memories",
    side: "right",
  },
  {
    title: "知识库 · 沉淀业务知识",
    description: "上传文档自动沉淀为结构化知识，回答自带出处，业务口径始终一致。",
    route: EnterpriseRoute.Knowledge,
    target: "route-/enterprise/knowledge",
    side: "right",
  },
  {
    title: "技能 · 扩展它会做的事",
    description: "这里是通用技能（Skills），扩展数字员工会做的事，支持从 GitHub 等开源社区直接导入。",
    route: EnterpriseRoute.GeneralSkills,
    target: "route-/enterprise/general-skills",
    side: "right",
  },
  {
    title: "SOP · 流程型技能",
    description: "SOP 是流程型技能：数字员工按步骤执行业务流程，可打断、可恢复，比固定的 workflow 更灵活。一句话描述需求，即可快速生成 SOP。",
    route: EnterpriseRoute.Skills,
    target: "route-/enterprise/skills",
    side: "right",
  },
  {
    title: "去对话端开始协作",
    description: "一切就绪！去对话端选择数字员工开始对话",
    route: EnterpriseRoute.Skills,
    target: "open-chat",
    side: "right",
    nextLabel: "开始对话",
    nextRoute: EnterpriseRoute.Gallery,
  },
];

function findVisibleTarget(targetName: string) {
  return Array.from(document.querySelectorAll<HTMLElement>(`[data-guide-target="${targetName}"]`)).find((element) => {
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  });
}

function findStepTarget(step: QuickStartStep) {
  return findVisibleTarget(step.target) || (step.fallbackTarget ? findVisibleTarget(step.fallbackTarget) : undefined);
}

export default function QuickStartGuide({ isAdmin }: { isAdmin: boolean }) {
  const navigate = useNavigate();
  const location = useLocation();
  const steps = useMemo(() => (isAdmin ? STEPS : STEPS.slice(1)), [isAdmin]);
  const [open, setOpen] = useState(false);
  const [step, setStep] = useState(0);
  const [anchorRect, setAnchorRect] = useState({ top: 0, left: 0, width: 1, height: 1 });
  const [anchorReady, setAnchorReady] = useState(false);

  useEffect(() => {
    const welcomeSeen = window.localStorage.getItem(ONBOARDING_SEEN_KEY);
    const quickStartSeen = window.localStorage.getItem(QUICK_START_SEEN_KEY);
    if (welcomeSeen && !quickStartSeen) setOpen(true);
  }, []);

  useEffect(() => {
    const reopen = () => {
      if (window.localStorage.getItem(QUICK_START_SEEN_KEY)) return;
      setStep(0);
      setOpen(true);
    };
    window.addEventListener(OPEN_QUICK_START_EVENT, reopen);
    return () => window.removeEventListener(OPEN_QUICK_START_EVENT, reopen);
  }, []);

  function finish() {
    window.localStorage.setItem(QUICK_START_SEEN_KEY, "1");
    setOpen(false);
    window.dispatchEvent(new Event(QUICK_START_COMPLETED_EVENT));
  }

  function goNext() {
    if (step === steps.length - 1) {
      if (current.nextRoute) navigate(current.nextRoute);
      finish();
    } else setStep((current) => current + 1);
  }

  function goPrev() {
    setStep((current) => Math.max(0, current - 1));
  }

  function runAction() {
    const current = steps[step];
    navigate(current.route);
    const eventName = current.eventName;
    if (eventName) {
      window.setTimeout(() => window.dispatchEvent(new Event(eventName)), 0);
    } else {
      window.setTimeout(() => findStepTarget(current)?.click(), 50);
    }
  }

  const current = steps[step];
  const isLast = step === steps.length - 1;

  useEffect(() => {
    if (open && location.pathname !== current.route) navigate(current.route);
  }, [current.route, location.pathname, navigate, open]);

  useLayoutEffect(() => {
    if (!open) return undefined;

    let frame = 0;
    let fallbackAllowed = false;
    setAnchorReady(false);
    const updateAnchor = () => {
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(() => {
        const target = findVisibleTarget(current.target)
          || (fallbackAllowed && current.fallbackTarget
            ? findVisibleTarget(current.fallbackTarget)
            : undefined);
        if (!target) return;
        const rect = target.getBoundingClientRect();
        setAnchorRect({ top: rect.top, left: rect.left, width: rect.width, height: rect.height });
        setAnchorReady(true);
      });
    };

    updateAnchor();
    const delayed = window.setTimeout(updateAnchor, 120);
    const fallbackDelay = window.setTimeout(() => {
      fallbackAllowed = true;
      updateAnchor();
    }, 700);
    const observer = new MutationObserver(updateAnchor);
    observer.observe(document.body, { childList: true, subtree: true });
    window.addEventListener("resize", updateAnchor);
    window.addEventListener("scroll", updateAnchor, true);
    return () => {
      window.clearTimeout(delayed);
      window.clearTimeout(fallbackDelay);
      window.cancelAnimationFrame(frame);
      observer.disconnect();
      window.removeEventListener("resize", updateAnchor);
      window.removeEventListener("scroll", updateAnchor, true);
    };
  }, [current.fallbackTarget, current.target, location.pathname, open]);

  return (
    <Popover open={open} onOpenChange={(next) => !next && finish()} modal>
      {open && <div aria-hidden="true" className="fixed inset-0 z-40 cursor-default bg-transparent" />}
      <PopoverAnchor asChild>
        <span aria-hidden="true" className="pointer-events-none fixed z-40" style={anchorRect} />
      </PopoverAnchor>
      <PopoverContent
        side={current.side || "bottom"}
        align="center"
        sideOffset={16}
        collisionPadding={12}
        avoidCollisions
        onInteractOutside={(event) => event.preventDefault()}
        className={`z-50 flex w-[434px] max-w-[calc(100vw-24px)] flex-col gap-[16px] rounded-[20px] border-0 bg-[rgba(24,24,26,0.8)] p-[24px] text-white shadow-[0_18px_60px_rgba(0,0,0,0.24)] ring-0 ${anchorReady ? "visible" : "invisible pointer-events-none"}`}
      >
        <PopoverArrow width={22} height={11} className="fill-[rgba(24,24,26,0.8)]" />
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          aria-label="关闭引导"
          onClick={finish}
          className="absolute top-[18px] right-[18px] text-white hover:bg-white/10 hover:text-white"
        >
          <XIcon className="size-[18px]" />
        </Button>
        <div className="flex min-h-[86px] flex-col gap-[4px] pb-[32px]">
          <PopoverTitle className="text-[14px] leading-[22px] font-medium text-white">
            {current.title}
          </PopoverTitle>
          <PopoverDescription className="text-[14px] leading-[22px] font-normal text-[#f6f6f6]">
            {current.description}
          </PopoverDescription>
        </div>

        <div className="flex items-center justify-between gap-[16px]">
          <span className="shrink-0 py-[3px] text-[14px] leading-normal text-[#858b9c]">
            {step + 1} / {steps.length}
          </span>
          <div className="flex min-w-0 items-center gap-[16px] max-[420px]:gap-[8px]">
            <Button
              variant="outline"
              onClick={step === 0 ? runAction : goPrev}
              className="h-[34px] min-w-[100px] rounded-[10px] border-[0.5px] border-[#6d6d6d] bg-black/20 px-[20px] text-[14px] leading-[22px] font-normal whitespace-nowrap text-white hover:bg-white/10 hover:text-white"
            >
              {step === 0 ? "立即添加" : "上一步"}
            </Button>
            <Button
              onClick={goNext}
              className="h-[34px] min-w-[100px] rounded-[8px] bg-white px-[16px] text-[14px] leading-[22px] font-normal whitespace-nowrap text-[#29282d] hover:bg-[#f0f0f0] hover:text-[#29282d]"
            >
              {current.nextLabel || (isLast ? "完成" : "下一步")}
            </Button>
          </div>
        </div>
      </PopoverContent>
    </Popover>
  );
}
