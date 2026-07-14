import profilePersona from "../assets/landing/hv-persona.png";
import innovationCard from "../assets/landing/dash-innovation-card@2x.png";
import featModules from "../assets/landing/dash-feat-modules@2x.png";
import featOnline from "../assets/landing/dash-feat-online@2x.png";
import featCreate from "../assets/landing/dash-feat-create@2x.png";
import cardTools from "../assets/staffdeck/sd1-card-tools.png";
import cardScheduled from "../assets/staffdeck/sd1-card-scheduled.png";
import cardLogs from "../assets/staffdeck/sd1-card-logs.png";
import { CalendarDays, ChevronLeft, ChevronRight } from "lucide-react";
import { useI18n } from "../i18n";
import copyByLocale from "../i18n/site.json";

const FEATURE_TABS = [
  { id: "innovation", active: true },
  { id: "modules" },
  { id: "online" },
  { id: "create" },
] as const;

const SMALL_CARDS = [
  { src: featModules, alt: "6大功能模块：技能 · 知识 · 工具 · 定时任务 · 可观测 · 记忆" },
  { src: featOnline, alt: "7X24全天候在线：主动履职，不用等人来问" },
  { src: featCreate, alt: "1句话创建SOP：可打断、可恢复、可多线并行" },
] as const;

function TabIcon({ type }: { type: (typeof FEATURE_TABS)[number]["id"] }) {
  switch (type) {
    case "innovation":
      return (
        <svg viewBox="0 0 14 14" aria-hidden>
          <rect x="2" y="3" width="10" height="9" rx="1.5" stroke="currentColor" fill="none" strokeWidth="1.2" />
          <path d="M2 6h10" stroke="currentColor" strokeWidth="1.2" />
          <path d="M5 1.5v2M9 1.5v2" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
        </svg>
      );
    case "modules":
      return (
        <svg viewBox="0 0 14 14" aria-hidden>
          <rect x="1.5" y="4" width="11" height="8" rx="1.2" stroke="currentColor" fill="none" strokeWidth="1.2" />
          <path d="M4.5 4V3a2.5 2.5 0 0 1 5 0v1" stroke="currentColor" strokeWidth="1.2" fill="none" />
        </svg>
      );
    case "online":
      return (
        <svg viewBox="0 0 14 14" aria-hidden>
          <circle cx="7" cy="7.5" r="5" stroke="currentColor" fill="none" strokeWidth="1.2" />
          <path d="M7 4.5v3.2l2 1.2" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
        </svg>
      );
    case "create":
      return (
        <svg viewBox="0 0 14 14" aria-hidden>
          <path d="M2 10.5l7.5-7.5 2 2L4 12.5H2v-2z" stroke="currentColor" fill="none" strokeWidth="1.1" strokeLinejoin="round" />
          <path d="M8.5 3.5l2 2" stroke="currentColor" strokeWidth="1.1" />
        </svg>
      );
  }
}

export default function DashOverview({ handoff = 1 }: { handoff?: number }) {
  const { locale } = useI18n();
  const copy = copyByLocale[locale].dashboard;
  const isEnglish = locale === "en-US";
  const personaOpacity = handoff >= 1
    ? 1
    : handoff >= 0.88
      ? (handoff - 0.88) / 0.12
      : 0;

  return (
    <div className="lp-do">
      <div className="lp-do-left">
        <section className="lp-do-profile">
          <div className="lp-do-greet">
            <img
              className="lp-do-persona"
              src={profilePersona}
              alt=""
              style={{ opacity: personaOpacity }}
            />
            <div className="lp-do-greet-copy">
              <p className="lp-do-greet-title">{copy.greeting}</p>
              <p className="lp-do-greet-sub">{copy.greetingSub}</p>
            </div>
          </div>

          <div className="lp-do-meta">
            <div className="lp-do-role">
              <p className="lp-do-role-text">
                {copy.role}
              </p>
              <div className="lp-do-tags">
                {copy.tags.map((tag, i) => (
                  <span className="lp-do-tag" key={`${tag}-${i}`}>
                    {tag}
                  </span>
                ))}
              </div>
            </div>
            <div className="lp-do-stats">
              {copy.stats.map((s, i) => (
                <div
                  className="lp-do-stat"
                  data-edge={i === 0 ? "start" : i === copy.stats.length - 1 ? "end" : "mid"}
                  key={s.label}
                >
                  <span className="lp-do-stat-val">{s.value}</span>
                  <span className="lp-do-stat-label">{s.label}</span>
                </div>
              ))}
            </div>
          </div>
        </section>

        <nav className="lp-do-tabs" role="tablist" aria-label={isEnglish ? "Capability categories" : "能力分类"}>
          {FEATURE_TABS.map((tab, index) => (
            <span
              className="lp-do-tab"
              data-active={"active" in tab && tab.active}
              key={tab.id}
              role="tab"
              aria-selected={"active" in tab && tab.active}
            >
              <TabIcon type={tab.id} />
              {copy.tabs[index]}
            </span>
          ))}
        </nav>

        {isEnglish ? (
          <section className="lp-do-features lp-do-features--html">
            <article className="lp-do-feature-card lp-do-feature-card--large">
              <div>
                <strong>{copy.innovationTitle}</strong>
                <p>{copy.innovationBody}</p>
              </div>
              <img src={cardTools} alt="" />
            </article>
            <div className="lp-do-feature-stack">
              {copy.features.map((feature, index) => (
                <article className="lp-do-feature-card" key={feature.title}>
                  <div>
                    <strong>{feature.title}</strong>
                    <p>{feature.body}</p>
                  </div>
                  <img src={[cardTools, cardScheduled, cardLogs][index]} alt="" />
                </article>
              ))}
            </div>
          </section>
        ) : (
          <section className="lp-do-features">
            <img
              className="lp-do-feat-img lp-do-feat-img--large"
              src={innovationCard}
              alt="3大核心创新：数字员工档案 · SOP状态机 · OKF知识本体"
            />
            <div className="lp-do-feat-stack">
              {SMALL_CARDS.map((card) => (
                <img
                  className="lp-do-feat-img"
                  src={card.src}
                  alt={card.alt}
                  key={card.alt}
                />
              ))}
            </div>
          </section>
        )}
      </div>

      <aside className="lp-do-right">
        <div className="lp-do-right-head">
          <div className="lp-do-worklog-head">
            <svg viewBox="0 0 14 14" aria-hidden>
              <rect x="2.5" y="2" width="9" height="11" rx="1.5" fill="none" stroke="currentColor" strokeWidth="1.2" />
              <path d="M5 2V1.2h4V2" fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
              <path d="M4.8 6h4.4M4.8 9h3" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
            </svg>
            {copy.workRecord}
          </div>
        </div>
        <div className="lp-do-worklog-card">
          <div className="lp-do-worklog-metrics">
            {copy.workMetrics.map((metric, index) => (
              <div className="lp-do-worklog-metric" data-tone={index > 1 ? index === 2 ? "positive" : "negative" : "neutral"} key={metric.label}>
                <strong>{metric.value}</strong>
                <span>{metric.label}</span>
              </div>
            ))}
          </div>
          <div className="lp-do-worklog-toolbar">
            <span className="lp-do-worklog-date">
              <CalendarDays aria-hidden />
              {copy.workDate}
            </span>
            <span className="lp-do-worklog-range">
              <ChevronLeft aria-hidden />
              <span>{copy.workRange}</span>
              <ChevronRight aria-hidden />
            </span>
            <span className="lp-do-worklog-periods">
              {copy.periods.map((period, index) => (
                <span data-active={index === 0} key={period}>{period}</span>
              ))}
            </span>
          </div>
          <div className="lp-do-worklog-chart" aria-hidden="true">
            {copy.chartLabels.map((label, index) => (
              <span
                className={`lp-do-worklog-bar lp-do-worklog-bar--${["primary", "secondary", "tertiary"][index]}`}
                key={label}
              >
                <i />
                {label}
              </span>
            ))}
            <div className="lp-do-worklog-hours">
              {copy.workHours.map((hour) => <span key={hour}>{hour}</span>)}
            </div>
          </div>
          <div className="lp-do-worklog-summaries">
            {copy.workSummaries.map((summary, index) => (
              <div data-tone={["neutral", "accent", "positive", "negative"][index]} key={summary.label}>
                <strong>{summary.value}</strong>
                <span>{summary.label}</span>
              </div>
            ))}
          </div>
          <div className="lp-do-worklog-filters">
            {copy.workFilters.map((filter, index) => (
              <span data-active={index === 0} key={filter}>{filter}</span>
            ))}
          </div>
          <div className="lp-do-worklog-table">
            <div className="lp-do-worklog-row lp-do-worklog-row--head">
              {copy.tableHeaders.map((header) => <span key={header}>{header}</span>)}
            </div>
            {copy.workRows.map((row, rowIndex) => (
              <div className="lp-do-worklog-row" key={`${row[0]}-${rowIndex}`}>
                {row.map((cell, cellIndex) => (
                  <span
                    data-status={cellIndex === 1 ? rowIndex === 1 ? "negative" : "positive" : undefined}
                    key={`${cell}-${cellIndex}`}
                  >
                    {cell}
                  </span>
                ))}
              </div>
            ))}
          </div>
        </div>
      </aside>
    </div>
  );
}
