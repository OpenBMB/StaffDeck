import { useEffect, useState, type ReactNode } from "react";
import {
  Brain,
  ChevronLeft,
  ChevronRight,
  IdCard,
  Workflow,
  XIcon,
} from "lucide-react";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import galleryImage from "@/assets/onboarding-gallery.png";
import profileImage from "@/assets/onboarding-profile.png";

export const ONBOARDING_SEEN_KEY = "staffdeck_onboarding_guide_seen";

/** Custom event that lets any part of the app re-open the onboarding guide. */
export const OPEN_ONBOARDING_EVENT = "staffdeck-open-onboarding";
export const OPEN_QUICK_START_EVENT = "staffdeck-open-quick-start";

type GuideCard = {
  icon: ReactNode;
  title: string;
  description: string;
};

type GuideStep = {
  image: string;
  eyebrow: string;
  titleLines: string[];
  description: string;
  cards: GuideCard[];
};

const CARD_ICON_CLASS = "size-[18px] text-white";
const CARD_BADGE_CLASS =
  "font-['Alimama_ShuHeiTi',_sans-serif] text-[16px] font-bold text-white";

const STEPS: GuideStep[] = [
  {
    image: galleryImage,
    eyebrow: "欢迎使用 StaffDeck",
    titleLines: ["数字员工", "全流程构建与管理平台"],
    description:
      "像招聘、培养、管理真人员工一样，构建你的数字员工团队。把重复的事情交给数字员工，让自己专注更重要的工作。",
    cards: [
      {
        icon: <IdCard className={CARD_ICON_CLASS} />,
        title: "像管员工一样管AI",
        description: "每位数字员工都有档案、岗位与成长记录。",
      },
      {
        icon: <Workflow className={CARD_ICON_CLASS} />,
        title: "按流程执行任务",
        description: "每位数字员工都有档案、岗位与成长记录。",
      },
      {
        icon: <Brain className={CARD_ICON_CLASS} />,
        title: "理解业务而非检索",
        description: "每位数字员工都有档案、岗位与成长记录。",
      },
    ],
  },
  {
    image: profileImage,
    eyebrow: "核心概念",
    titleLines: ["三步搭建你的数字员工"],
    description: "先给它配大脑，再给它配能力，最后上岗对话。",
    cards: [
      {
        icon: <span className={CARD_BADGE_CLASS}>01</span>,
        title: "模型",
        description: "数字员工的大脑，接入 OpenAI 兼容模型即可。",
      },
      {
        icon: <span className={CARD_BADGE_CLASS}>02</span>,
        title: "能力",
        description: "知识库、技能、SOP、工具，决定它懂什么、会做什么。",
      },
      {
        icon: <span className={CARD_BADGE_CLASS}>03</span>,
        title: "上岗",
        description: "创建数字员工并绑定能力，去对话端与它协作。",
      },
    ],
  },
];

export default function OnboardingGuide() {
  const [open, setOpen] = useState(false);
  const [step, setStep] = useState(0);

  useEffect(() => {
    const seen = window.localStorage.getItem(ONBOARDING_SEEN_KEY);
    if (!seen) {
      setStep(0);
      setOpen(true);
    }
  }, []);

  useEffect(() => {
    const reopen = () => {
      setStep(0);
      setOpen(true);
    };
    window.addEventListener(OPEN_ONBOARDING_EVENT, reopen);
    return () => window.removeEventListener(OPEN_ONBOARDING_EVENT, reopen);
  }, []);

  function finish() {
    window.localStorage.setItem(ONBOARDING_SEEN_KEY, "1");
    setOpen(false);
    window.dispatchEvent(new Event(OPEN_QUICK_START_EVENT));
  }

  function goPrev() {
    setStep((prev) => Math.max(0, prev - 1));
  }

  function goNext() {
    if (step >= STEPS.length - 1) {
      finish();
    } else {
      setStep((prev) => Math.min(STEPS.length - 1, prev + 1));
    }
  }

  function handleOpenChange(next: boolean) {
    if (!next) finish();
    else setOpen(true);
  }

  const current = STEPS[step];
  const isFirst = step === 0;
  const isLast = step === STEPS.length - 1;

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        showCloseButton={false}
        className="grid w-[904px] max-w-[calc(100vw-2rem)] grid-cols-1 gap-0 overflow-hidden rounded-[20px] border-0 p-0 ring-0 md:grid-cols-[474px_430px] sm:max-w-[904px]"
      >
        <DialogTitle className="sr-only">
          {current.titleLines.join("")}
        </DialogTitle>

        <div className="hidden h-[560px] bg-[#e9eef6] md:block">
          <img
            key={current.image}
            src={current.image}
            alt=""
            className="size-full object-cover object-top-left"
          />
        </div>

        <div className="relative flex h-[560px] flex-col justify-between bg-linear-to-b from-[#f9fcff] to-[#e3f1ff] px-[36px] pt-[10px] pb-[32px]">
          <div className="flex items-center justify-end">
            <button
              type="button"
              onClick={finish}
              aria-label="关闭引导"
              className="flex size-[20px] items-center justify-center text-[#757f9c] transition-colors hover:text-[#18181a]"
            >
              <XIcon className="size-[14px]" />
            </button>
          </div>

          <div className="flex flex-col gap-[24px]">
            <div className="flex flex-col gap-[4px]">
              <span className="-skew-x-6 text-[12px] leading-none text-[#464c5e]">
                {current.eyebrow}
              </span>
              <div className="-skew-x-6">
                {current.titleLines.map((line) => (
                  <p
                    key={line}
                    className="bg-linear-to-r from-[#105acf] to-[#007bff] bg-clip-text text-[32px] leading-[44px] font-semibold text-transparent"
                  >
                    {line}
                  </p>
                ))}
              </div>
              <p className="text-[12px] leading-[20px] text-[#757f9c]">
                {current.description}
              </p>
            </div>

            <div className="flex flex-col gap-[12px]">
              {current.cards.map((card) => (
                <div
                  key={card.title}
                  className="flex items-center gap-[8px] rounded-[14px] bg-white/60 px-[12px] py-[10px]"
                >
                  <div className="flex size-[32px] shrink-0 items-center justify-center rounded-[8px] bg-linear-to-br from-[#89b6ff] to-[#527aff]">
                    {card.icon}
                  </div>
                  <div className="flex min-w-0 flex-col gap-[4px]">
                    <p className="truncate text-[14px] leading-none text-[#464c5e]">
                      {card.title}
                    </p>
                    <p className="truncate text-[12px] leading-none text-[#757f9c]">
                      {card.description}
                    </p>
                  </div>
                </div>
              ))}
            </div>
          </div>

          <div className="flex items-center justify-between">
            <div className="flex items-center gap-[4px] text-[#757f9c]">
              <button
                type="button"
                onClick={goPrev}
                disabled={isFirst}
                aria-label="上一步"
                className="flex size-[14px] items-center justify-center transition-colors enabled:hover:text-[#18181a] disabled:cursor-default disabled:opacity-40"
              >
                <ChevronLeft className="size-[14px]" />
              </button>
              <span className="text-[12px]">
                {step + 1}/{STEPS.length}
              </span>
              <button
                type="button"
                onClick={goNext}
                disabled={isLast}
                aria-label="下一步"
                className="flex size-[14px] items-center justify-center transition-colors enabled:hover:text-[#18181a] disabled:cursor-default disabled:opacity-40"
              >
                <ChevronRight className="size-[14px]" />
              </button>
            </div>

            <div className="flex items-center gap-[12px]">
              <button
                type="button"
                onClick={finish}
                className="flex w-[80px] items-center justify-center rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[20px] py-[8px] text-[14px] text-[#757f9c] transition-colors hover:bg-[#f6f6f6] hover:text-[#18181a]"
              >
                跳过
              </button>
              <button
                type="button"
                onClick={goNext}
                className="flex w-[134px] items-center justify-center rounded-[10px] bg-[#18181a] px-[32px] py-[8px] text-[14px] text-white transition-colors hover:bg-[#303030]"
              >
                {isLast ? "开始使用" : "下一步"}
              </button>
            </div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
