import type { ReactNode } from 'react';

import { useGridWaveReveal, useStaggerReveal } from '@/lib/motion';

export type RevealGroupProps = {
  children: ReactNode;
  className?: string;
  /**
   * 子元素数量。首次 `> 0` 时播放一次 stagger；后续筛选/翻页导致的增减
   * 不会再触发，避免列表反复跳动。
   */
  itemCount: number;
  /** 起始纵向位移（px）。默认线性 12 / 波 18。 */
  y?: number;
  /** 子元素之间的 stagger 步长（ms）。默认线性 36 / 波 90。 */
  stagger?: number;
  /** 单次动画时长（ms）。默认 `MOTION.base`（波为 560）。 */
  duration?: number;
  /** 变化时重播一次入场（例如开放平台的 tab 切换）；留空则只在首次播放。 */
  replayKey?: string | number;
  /**
   * 网格放射波：延迟按容器真实列数二维计算，从中心向外扩散。
   * 仅用于网格布局（列数从 `grid-template-columns` 读），列表请用默认线性 stagger。
   */
  wave?: boolean;
  'aria-label'?: string;
};

/**
 * 给网格/列表容器加一次首屏 stagger 入场。
 *
 * 渲染结果就是一个普通的 `<div className=...>`，DOM 结构与直接写 `<div>` 完全一致，
 * 因此不会影响布局或既有测试；动效只在首次有子元素时播放。
 */
export function RevealGroup({
  children,
  className,
  itemCount,
  y,
  stagger,
  duration,
  replayKey,
  wave = false,
  'aria-label': ariaLabel,
}: RevealGroupProps) {
  const enabled = itemCount > 0;
  // 网格放射波要「看得见」：步长与时长都比线性入场大一档，位移也更明显；
  // 未传参数时按 wave 给出各自默认值，其它页面的线性入场行为完全不变。
  const step = stagger ?? (wave ? 90 : 36);
  const offsetY = y ?? (wave ? 18 : 12);
  const len = duration ?? (wave ? 560 : undefined);
  // 两个 hook 都要无条件调用（Hook 规则），实际只用一个：
  // 线性版保持「只播一次」，波版承担 tab 切换重播，两者都用 replayKey 锁住语义。
  const linearRef = useStaggerReveal<HTMLDivElement>(enabled && !wave, {
    y: offsetY,
    stagger: step,
    duration: len,
    replayKey,
  });
  const waveRef = useGridWaveReveal<HTMLDivElement>(enabled && wave, {
    y: offsetY,
    stagger: step,
    duration: len,
    replayKey,
  });

  return (
    <div ref={wave ? waveRef : linearRef} className={className} aria-label={ariaLabel}>
      {children}
    </div>
  );
}