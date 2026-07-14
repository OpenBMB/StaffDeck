import {
  memo,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";
import {
  Apple,
  BookOpen,
  ChevronDown,
  Download as DownloadIcon,
  Monitor,
  Terminal,
} from "lucide-react";

import AppHeader from "../components/AppHeader";
import BrandLogo from "../components/BrandLogo";
import LanguageSwitcher from "../components/LanguageSwitcher";
import { useI18n } from "../i18n";
import copyByLocale from "../i18n/site.json";
import logoMark from "../assets/LOGO.svg";
import icKnowledge from "../assets/landing/knowledge.svg";
import icChat from "../assets/landing/chat.svg";
import icSkill from "../assets/landing/skill.svg";
import icText from "../assets/landing/text.svg";
import icSop from "../assets/landing/sop.svg";
import icTool from "../assets/landing/tool.svg";
import hvDashboard from "../assets/staffdeck/login-preview.png";
import hvPersona from "../assets/landing/hv-persona.png";
import visualBg from "../assets/landing/visual-bg.svg?url";
import advantage1 from "../assets/landing/advantage-1.png";
import advantage2 from "../assets/landing/advantage-2.png";
import advantage3 from "../assets/landing/advantage-3.png";
import DashOverview from "./DashOverview";
import SiteChat from "./SiteChat";
import "./landing.css";

type RevealStyle = CSSProperties & { "--d"?: string };

/** Delay helper for staggered reveals. */
function d(ms: number): RevealStyle {
  return { "--d": String(ms) };
}

const SCENE_COUNT = 3;
const REPOSITORY_URL = "https://github.com/OpenBMB/URStaff";
const TUTORIAL_URL = import.meta.env.VITE_STAFFDECK_DOCS_URL
  || "https://github.com/OpenBMB/URStaff/blob/main/README_zh.md";
const DOWNLOAD_URL = "https://github.com/OpenBMB/URStaff/releases/latest";

function GitHubIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" fill="currentColor">
      <path d="M12 2C6.48 2 2 6.59 2 12.25c0 4.53 2.87 8.37 6.84 9.73.5.09.68-.22.68-.49v-1.91c-2.78.62-3.37-1.21-3.37-1.21-.45-1.19-1.11-1.51-1.11-1.51-.91-.63.07-.62.07-.62 1 .08 1.53 1.06 1.53 1.06.89 1.57 2.34 1.11 2.91.85.09-.66.35-1.11.63-1.37-2.22-.26-4.56-1.14-4.56-5.07 0-1.12.39-2.03 1.03-2.75-.1-.26-.45-1.3.1-2.71 0 0 .84-.28 2.75 1.05A9.3 9.3 0 0 1 12 6.96a9.2 9.2 0 0 1 2.5.35c1.91-1.33 2.75-1.05 2.75-1.05.55 1.41.2 2.45.1 2.71.64.72 1.03 1.63 1.03 2.75 0 3.94-2.34 4.8-4.57 5.06.36.32.68.94.68 1.89v2.82c0 .27.18.59.69.49A10.23 10.23 0 0 0 22 12.25C22 6.59 17.52 2 12 2Z" />
    </svg>
  );
}

/* Floating hero icons. Order maps to .lp-orb--1..6 in landing.css, which owns
   each icon's position/size/rotation/shadow so it can adapt responsively. */
const HERO_ICONS = [icKnowledge, icChat, icSkill, icSop, icText, icTool];

/* Hero orb -> dash rail slot handoff (scene 1 -> scene 2). Orbs 2-3 fade out. */
const ICON_HANDOFF = [
  { hero: 0, rail: 0, rot: -26.86 },
  { hero: 4, rail: 1, rot: 14.62 },
  { hero: 3, rail: 2, rot: 25.04 },
  { hero: 5, rail: 3, rot: 18.83 },
] as const;
const HERO_FADE_ORBS = new Set([1, 2]);

type FlyerPose = {
  hero: number;
  left: number;
  top: number;
  width: number;
  height: number;
  rotate: number;
  opacity: number;
};

type PersonaPose = {
  left: number;
  top: number;
  width: number;
  height: number;
  rotate: number;
  opacity: number;
};

type HandoffFrame = {
  progress: number;
  flyers: FlyerPose[];
  persona: PersonaPose | null;
};

function smoothstep(t: number) {
  return t * t * (3 - 2 * t);
}

const MemoSiteChat = memo(SiteChat);

/* The pinned "story" screen steps through these states while the dashboard
   window stays put: step 0 is the 资料 overview, steps 1-3 swap the feature
   card and slide the rail selection down the 能力 icons. */
const ADVANTAGE_IMAGES = [advantage1, advantage2, advantage3];

export default function LandingPage() {
  const { locale } = useI18n();
  const copy = copyByLocale[locale];
  const advantages = copy.story.advantages.map((advantage, index) => ({
    ...advantage,
    img: ADVANTAGE_IMAGES[index],
  }));
  const rootRef = useRef<HTMLDivElement>(null);
  const sectionRefs = useRef<Array<HTMLElement | null>>([]);
  const stepRefs = useRef<Array<HTMLDivElement | null>>([]);
  const [active, setActive] = useState(0);
  const [dashStep, setDashStep] = useState(0);
  const [scrolled, setScrolled] = useState(false);
  const activeSceneRef = useRef(0);
  const dashStepRef = useRef(0);
  const scrolledRef = useRef(false);
  const [handoffFrame, setHandoffFrame] = useState<HandoffFrame>({
    progress: 0,
    flyers: [],
    persona: null,
  });
  const orbRefs = useRef<Array<HTMLDivElement | null>>([]);
  const railSlotRefs = useRef<Array<HTMLDivElement | null>>([]);
  const handoffFromCache = useRef<Map<number, DOMRect>>(new Map());
  const personaRef = useRef<HTMLImageElement>(null);
  const overviewRef = useRef<HTMLDivElement>(null);
  const personaFromCache = useRef<DOMRect | null>(null);
  const prevActiveRef = useRef(0);

  const handoff = handoffFrame.progress;
  const flyers = handoffFrame.flyers;
  const personaFlyer = handoffFrame.persona;

  const scrollToScene = (index: number) => {
    sectionRefs.current[index]?.scrollIntoView({ behavior: "smooth" });
  };

  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;

    // Center-band detector: whichever section crosses the viewport midline is
    // the active one. Works for any section height (incl. the tall pinned
    // story track, which a fixed ratio threshold could never satisfy).
    const sections = sectionRefs.current.filter(Boolean) as HTMLElement[];
    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          const idx = Number((entry.target as HTMLElement).dataset.index);
          if (entry.isIntersecting) {
            entry.target.classList.add("is-active");
            if (activeSceneRef.current !== idx) {
              activeSceneRef.current = idx;
              setActive(idx);
            }
          } else {
            entry.target.classList.remove("is-active");
          }
        });
      },
      { root, rootMargin: "-49% 0px -49% 0px", threshold: 0 },
    );
    sections.forEach((section) => observer.observe(section));

    // Track which step of the pinned "story" screen is centred, so the rail
    // selection + feature card can update while the window stays pinned.
    const steps = stepRefs.current.filter(Boolean) as HTMLElement[];
    const stepObserver = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting && entry.intersectionRatio >= 0.5) {
            const nextStep = Number((entry.target as HTMLElement).dataset.step);
            if (dashStepRef.current !== nextStep) {
              dashStepRef.current = nextStep;
              setDashStep(nextStep);
            }
          }
        });
      },
      { root, threshold: [0.5] },
    );
    steps.forEach((step) => stepObserver.observe(step));

    const onScroll = () => {
      const nextScrolled = root.scrollTop > 24;
      if (scrolledRef.current !== nextScrolled) {
        scrolledRef.current = nextScrolled;
        setScrolled(nextScrolled);
      }
    };
    root.addEventListener("scroll", onScroll, { passive: true });

    // Kick off the hero entrance — deferred by two frames so the browser paints
    // the hidden start state first; otherwise the fly-in transition never plays.
    const first = sectionRefs.current[0];
    let raf1 = 0;
    let raf2 = 0;
    if (first) {
      raf1 = requestAnimationFrame(() => {
        raf2 = requestAnimationFrame(() => first.classList.add("is-active"));
      });
    }

    return () => {
      observer.disconnect();
      stepObserver.disconnect();
      root.removeEventListener("scroll", onScroll);
      cancelAnimationFrame(raf1);
      cancelAnimationFrame(raf2);
    };
  }, []);

  // Snapshot each hero element while the first screen is visible. These reads
  // happen in one animation frame and are reused as stable launch positions.
  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    let raf = 0;
    const snapshot = () => {
      raf = 0;
      if (root.scrollTop > root.clientHeight * 0.4) return;
      for (const { hero: heroIndex } of ICON_HANDOFF) {
        const rect = orbRefs.current[heroIndex]
          ?.querySelector(".lp-orb-tile")
          ?.getBoundingClientRect();
        if (rect && rect.width > 0) {
          handoffFromCache.current.set(heroIndex, rect);
        }
      }
      const personaRect = personaRef.current?.getBoundingClientRect();
      if (personaRect && personaRect.width > 0) {
        personaFromCache.current = personaRect;
      }
    };
    const scheduleSnapshot = () => {
      if (!raf) raf = requestAnimationFrame(snapshot);
    };
    snapshot();
    root.addEventListener("scroll", scheduleSnapshot, { passive: true });
    window.addEventListener("resize", scheduleSnapshot);
    return () => {
      root.removeEventListener("scroll", scheduleSnapshot);
      window.removeEventListener("resize", scheduleSnapshot);
      if (raf) cancelAnimationFrame(raf);
    };
  }, []);

  // Preserve the original 1100 ms handoff exactly. Every frame first reads all
  // live targets, then calculates every pose, and finally performs one state
  // update so React never commits between layout reads.
  useEffect(() => {
    if (active !== 1) {
      prevActiveRef.current = active;
      setHandoffFrame({ progress: 0, flyers: [], persona: null });
      return;
    }

    const fromChat = prevActiveRef.current === 2;
    prevActiveRef.current = active;

    if (fromChat) {
      setHandoffFrame({ progress: 1, flyers: [], persona: null });
      return;
    }

    const reduceMotion = window.matchMedia(
      "(prefers-reduced-motion: reduce)",
    ).matches;
    if (reduceMotion) {
      setHandoffFrame({ progress: 1, flyers: [], persona: null });
      return;
    }

    const legs = ICON_HANDOFF.map(({ hero: heroIndex, rail, rot }) => {
      let from = handoffFromCache.current.get(heroIndex);
      const slot = railSlotRefs.current[rail];
      if (!from && slot) {
        const rect = slot.getBoundingClientRect();
        from = new DOMRect(rect.left - 40, rect.top - 220, 84, 84);
      }
      return from && slot
        ? { hero: heroIndex, rail, from, rot }
        : null;
    }).filter(Boolean) as Array<{
      hero: number;
      rail: number;
      from: DOMRect;
      rot: number;
    }>;

    let personaFrom = personaFromCache.current;
    if (!personaFrom) {
      const overviewRect = overviewRef.current?.getBoundingClientRect();
      if (overviewRect) {
        personaFrom = new DOMRect(
          overviewRect.left + overviewRect.width * 0.03,
          overviewRect.top - 220,
          200,
          140,
        );
      }
    }

    if (legs.length === 0 && !personaFrom) {
      setHandoffFrame({ progress: 1, flyers: [], persona: null });
      return;
    }

    const duration = 1100;
    const crossfadeFrom = 0.88;
    const startTime = performance.now();
    let raf = 0;

    const tick = (now: number) => {
      const raw = Math.min(1, (now - startTime) / duration);
      const progress = smoothstep(raw);

      if (progress >= 1) {
        // Keep the final flyer frame for one paint, exactly as before, then
        // reveal the settled rail and overview elements.
        setHandoffFrame((frame) => ({ ...frame, progress: 1 }));
        raf = requestAnimationFrame(() => {
          setHandoffFrame({ progress: 1, flyers: [], persona: null });
        });
        return;
      }

      const flyerOpacity = progress >= crossfadeFrom
        ? (1 - progress) / (1 - crossfadeFrom)
        : 1;

      // Read all moving targets before any state write. The targets remain
      // live because the dashboard itself is still scaling into place.
      const personaTarget = (
        overviewRef.current?.querySelector(".lp-do-persona") as HTMLElement | null
      )?.getBoundingClientRect();
      const railTargets = legs.map(({ rail }) => {
        const slot = railSlotRefs.current[rail];
        return slot?.querySelector("img")?.getBoundingClientRect()
          ?? slot?.getBoundingClientRect();
      });

      let nextPersona: PersonaPose | null = null;
      if (personaFrom && personaTarget && personaTarget.width > 0) {
        const fromCx = personaFrom.left + personaFrom.width / 2;
        const fromCy = personaFrom.top + personaFrom.height / 2;
        const toCx = personaTarget.left + personaTarget.width / 2;
        const toCy = personaTarget.top + personaTarget.height / 2;
        const width = personaFrom.width
          + (personaTarget.width - personaFrom.width) * progress;
        const height = personaFrom.height
          + (personaTarget.height - personaFrom.height) * progress;
        nextPersona = {
          left: fromCx + (toCx - fromCx) * progress - width / 2,
          top: fromCy + (toCy - fromCy) * progress - height / 2,
          width,
          height,
          rotate: -8 * Math.pow(1 - progress, 2),
          opacity: flyerOpacity,
        };
      }

      const nextFlyers = legs.flatMap(({ hero, from, rot }, index) => {
        const target = railTargets[index];
        if (!target || target.width === 0) return [];
        const fromCx = from.left + from.width / 2;
        const fromCy = from.top + from.height / 2;
        const toCx = target.left + target.width / 2;
        const toCy = target.top + target.height / 2;
        const width = from.width + (target.width - from.width) * progress;
        const height = from.height + (target.height - from.height) * progress;
        return [{
          hero,
          left: fromCx + (toCx - fromCx) * progress - width / 2,
          top: fromCy + (toCy - fromCy) * progress - height / 2,
          width,
          height,
          rotate: rot * Math.pow(1 - progress, 2),
          opacity: flyerOpacity,
        }];
      });

      setHandoffFrame({
        progress,
        flyers: nextFlyers,
        persona: nextPersona,
      });
      raf = requestAnimationFrame(tick);
    };

    setHandoffFrame({ progress: 0, flyers: [], persona: null });
    raf = requestAnimationFrame(tick);
    return () => {
      if (raf) cancelAnimationFrame(raf);
    };
  }, [active]);

  // Keyboard navigation (↑ / ↓ / PageUp / PageDown). Inside the pinned story
  // screen we advance one step at a time; otherwise we jump between scenes.
  useEffect(() => {
    const root = rootRef.current;
    const onKey = (event: KeyboardEvent) => {
      const down = event.key === "ArrowDown" || event.key === "PageDown";
      const up = event.key === "ArrowUp" || event.key === "PageUp";
      if (!down && !up) return;
      event.preventDefault();
      const inStory = active === 1;
      if (down) {
        if (root && inStory && dashStep < ADVANTAGE_IMAGES.length) {
          root.scrollBy({ top: window.innerHeight, behavior: "smooth" });
        } else {
          scrollToScene(Math.min(active + 1, SCENE_COUNT - 1));
        }
      } else if (root && inStory && dashStep > 0) {
        root.scrollBy({ top: -window.innerHeight, behavior: "smooth" });
      } else {
        scrollToScene(Math.max(active - 1, 0));
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [active, dashStep]);

  const setSectionRef = (index: number) => (el: HTMLElement | null) => {
    sectionRefs.current[index] = el;
  };
  const setStepRef = (index: number) => (el: HTMLDivElement | null) => {
    stepRefs.current[index] = el;
  };
  const setOrbRef = (index: number) => (el: HTMLDivElement | null) => {
    orbRefs.current[index] = el;
  };
  const setRailSlotRef = (index: number) => (el: HTMLDivElement | null) => {
    railSlotRefs.current[index] = el;
  };

  const handoffHero = (index: number) =>
    ICON_HANDOFF.some((mapping) => mapping.hero === index);
  const orbHidden = handoff > 0.02 && handoff < 0.98
    ? (index: number) => handoffHero(index)
    : () => false;
  const orbFade = handoff > 0 && handoff < 1
    ? (index: number) => (
        HERO_FADE_ORBS.has(index) ? Math.max(0, 1 - handoff * 2.2) : 1
      )
    : () => 1;

  return (
    <div
      className="lp-root"
      ref={rootRef}
      data-scene={active}
      data-handoff={handoff > 0.02 ? "on" : "off"}
    >

      {/* Icons and persona retain the original handoff trajectory. */}
      {flyers.length > 0 && (
        <div className="lp-icon-handoff" aria-hidden>
          {flyers.map((flyer) => (
            <div
              key={flyer.hero}
              className="lp-icon-handoff__item"
              style={{
                left: flyer.left,
                top: flyer.top,
                width: flyer.width,
                height: flyer.height,
                opacity: flyer.opacity,
                transform: `rotate(${flyer.rotate}deg)`,
              }}
            >
              <img src={HERO_ICONS[flyer.hero]} alt="" />
            </div>
          ))}
        </div>
      )}

      {personaFlyer && (
        <div
          className="lp-persona-fly"
          aria-hidden
          style={{
            left: personaFlyer.left,
            top: personaFlyer.top,
            width: personaFlyer.width,
            height: personaFlyer.height,
            opacity: personaFlyer.opacity,
            transform: `rotate(${personaFlyer.rotate}deg)`,
          }}
        >
          <img src={hvPersona} alt="" />
        </div>
      )}

      {/* header — shared global AppHeader inside the fixed scroll-blur wrapper */}
      <div className="lp-header" data-solid={scrolled}>
        <AppHeader
          className="h-[60px] items-center px-[32px]"
          left={<BrandLogo markSize={28} />}
          right={<LanguageSwitcher />}
        />
      </div>

      {/* ============================================= SCENE 1 · HERO ==== */}
      <section
        className="lp-section lp-hero"
        data-index={0}
        ref={setSectionRef(0)}
      >
        <div className="lp-orbits" aria-hidden>
          {HERO_ICONS.map((src, i) => (
            <div
              key={i}
              className={`lp-orb lp-orb--${i + 1}`}
              ref={setOrbRef(i)}
              style={{
                opacity: orbHidden(i)
                  ? 0
                  : orbFade(i) < 1
                    ? orbFade(i)
                    : undefined,
              }}
            >
              <div className="lp-orb-float">
                <div className="lp-orb-tile">
                  <img src={src} alt="" />
                </div>
              </div>
            </div>
          ))}
        </div>

        <div className="lp-hero-inner">
          <div className="lp-hero-copy">
            <span className="lp-badge">
              <img src={logoMark} alt="" />
              StaffDeck
            </span>
            <h1 className="lp-title">
              StaffDeck
              <br />
              <span className="lp-line2">{copy.hero.platform}</span>
            </h1>
            <p className="lp-subtitle">{copy.hero.subtitle}</p>
            <div className="lp-hero-actions">
              <a className="lp-hero-action" href={REPOSITORY_URL} target="_blank" rel="noreferrer">
                <GitHubIcon />
                <span>{copy.hero.repository}</span>
              </a>
              <a className="lp-hero-action" href={TUTORIAL_URL} target="_blank" rel="noreferrer">
                <BookOpen aria-hidden />
                <span>{copy.hero.tutorial}</span>
              </a>
              <details className="lp-download-menu">
                <summary className="lp-hero-action lp-hero-action--primary">
                  <DownloadIcon aria-hidden />
                  <span>{copy.hero.download}</span>
                  <ChevronDown className="lp-download-chevron" aria-hidden />
                </summary>
                <div className="lp-download-options">
                  <a href={DOWNLOAD_URL} target="_blank" rel="noreferrer"><Apple aria-hidden />{copy.hero.mac}</a>
                  <a href={DOWNLOAD_URL} target="_blank" rel="noreferrer"><Terminal aria-hidden />{copy.hero.linux}</a>
                  <a href={DOWNLOAD_URL} target="_blank" rel="noreferrer"><Monitor aria-hidden />{copy.hero.windows}</a>
                </div>
              </details>
            </div>
          </div>
        </div>

        {/* illustration + bubble + dashboard, anchored to section bottom */}
        <div className="lp-hero-visual lp-reveal lp-reveal--rise" style={d(560)}>
          <img className="lp-hv-bg" src={visualBg} alt="" aria-hidden />
          <div className="lp-hv-scene">
            <img
              className="lp-hv-persona"
              src={hvPersona}
              alt="数字员工"
              ref={personaRef}
            />
            <span className="lp-hv-bubble">{copy.hero.bubble}</span>
          </div>
          <div className="lp-hv-dash">
            <img src={hvDashboard} alt={copy.hero.dashboardAlt} />
          </div>
        </div>
      </section>

      {/* ==================================== SCENE 2 · STORY (PINNED) ==== */}
      {/* One pinned screen: the dashboard window + rail stay put while the
          scroll advances through 4 steps. Only the right feature card and the
          rail selection change — no full-screen switch. */}
      <section
        className="lp-section lp-dash lp-dash-track"
        data-index={1}
        ref={setSectionRef(1)}
        data-step={dashStep}
      >
        <div className="lp-dash-sticky">
          <div className="lp-dash-inner">
            <div className="lp-dash-window lp-reveal lp-reveal--scale" style={d(60)}>
              <div className="lp-dash-stage">
                {/* floating left menu rail — the hero icons fly into these
                    slots as scene 1 hands off, then the pill slides down the
                    能力 icons as the story steps through. */}
                <DashRail
                  active={dashStep}
                  handoff={handoff}
                  labels={{ data: copy.story.data, capability: copy.story.capability }}
                  onSlotRef={setRailSlotRef}
                  onSelect={(i) =>
                    stepRefs.current[i]?.scrollIntoView({
                      behavior: "instant" as ScrollBehavior,
                      block: "start",
                    })
                  }
                />

                <div className="lp-dash-content lp-dash-content--feature">
                  {/* step 0 · 资料 overview */}
                  <div
                    className="lp-dash-overview"
                    data-on={dashStep === 0}
                    aria-hidden={dashStep !== 0}
                    ref={overviewRef}
                  >
                    <DashOverview handoff={handoff} />
                  </div>

                  {/* steps 1-3 · shared header, cross-fading feature cards */}
                  <div className="lp-featwin" data-on={dashStep >= 1}>
                    <h2 className="lp-featwin-title">
                      {copy.story.title}
                    </h2>
                    <p className="lp-featwin-sub">{copy.story.subtitle}</p>
                    <div className="lp-featwin-cards">
                      {advantages.map((a, i) => (
                        <div
                          className="lp-featwin-card"
                          data-on={dashStep === i + 1}
                          key={a.tag}
                        >
                          <div className="lp-featwin-illus">
                            <img src={a.img} alt={a.alt} />
                          </div>
                          <div className="lp-featwin-copy">
                            <span className="lp-featwin-tag">{a.tag}</span>
                            <h3>{a.title}</h3>
                            <p>{a.body}</p>
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>

        {/* invisible scroll markers drive the pinned sequence + snap points */}
        <div className="lp-dash-steps" aria-hidden>
          {[0, 1, 2, 3].map((s) => (
            <div
              className="lp-dash-step"
              data-step={s}
              key={s}
              ref={setStepRef(s)}
            />
          ))}
        </div>
      </section>

      {/* =========================================== SCENE 3 · CHAT ====== */}
      <section className="lp-section lp-chat" data-index={2} ref={setSectionRef(2)}>
        <div className="lp-chat-embed">
          <MemoSiteChat />
        </div>
      </section>
    </div>
  );
}

/** Shared dashboard menu rail. `handoff` cross-fades the arriving icons into
 *  their settled slots without changing the original timing. */
function DashRail({
  active,
  handoff,
  labels,
  onSlotRef,
  onSelect,
}: {
  active: number;
  handoff: number;
  labels: { data: string; capability: string };
  onSlotRef: (index: number) => (el: HTMLDivElement | null) => void;
  onSelect: (index: number) => void;
}) {
  const slotRefs = useRef<Array<HTMLDivElement | null>>([]);
  const [pillY, setPillY] = useState(0);
  const [pillVisible, setPillVisible] = useState(false);

  useLayoutEffect(() => {
    const measure = () => {
      const target = slotRefs.current[active];
      if (target) {
        const nextPillY = target.offsetTop;
        setPillY((current) => current === nextPillY ? current : nextPillY);
      }
    };
    measure();
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, [active]);

  const handoffComplete = handoff >= 0.999;
  useEffect(() => {
    if (!handoffComplete) {
      setPillVisible(false);
      return;
    }
    const timer = window.setTimeout(() => setPillVisible(true), 150);
    return () => window.clearTimeout(timer);
  }, [handoffComplete]);

  const setSlot = (i: number) => (el: HTMLDivElement | null) => {
    slotRefs.current[i] = el;
    onSlotRef(i)(el);
  };

  const slotOpacity = handoff >= 1
    ? 1
    : handoff >= 0.88
      ? (handoff - 0.88) / 0.12
      : 0;

  const railStyle = { "--pill-y": `${pillY}px` } as CSSProperties;

  const slotProps = (i: number, label: string) => ({
    className: "lp-dash-slot",
    ref: setSlot(i),
    role: "tab" as const,
    tabIndex: 0,
    "aria-label": label,
    "aria-selected": active === i,
    onClick: () => onSelect(i),
    onKeyDown: (e: ReactKeyboardEvent) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        onSelect(i);
      }
    },
  });

  return (
    <div className="lp-dash-rail" style={railStyle} role="tablist">
      <div
        className="lp-dash-pill"
        style={{ opacity: pillVisible ? 1 : 0 }}
        aria-hidden
      />
      <span className="lp-dash-rail-label" aria-hidden>{labels.data}</span>
      <div {...slotProps(0, labels.data)}>
        <img src={icKnowledge} alt="" style={{ opacity: slotOpacity }} />
      </div>
      <span className="lp-dash-rail-label lp-dash-rail-label--cap" aria-hidden>{labels.capability}</span>
      <div {...slotProps(1, `${labels.capability} 1`)}>
        <img src={icText} alt="" style={{ opacity: slotOpacity }} />
      </div>
      <div {...slotProps(2, `${labels.capability} 2`)}>
        <img src={icSop} alt="" style={{ opacity: slotOpacity }} />
      </div>
      <div {...slotProps(3, `${labels.capability} 3`)}>
        <img src={icTool} alt="" style={{ opacity: slotOpacity }} />
      </div>
    </div>
  );
}
