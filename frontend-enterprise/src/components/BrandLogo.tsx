import { cn } from '@/lib/utils';
import logoFull from '../assets/fosun-holiday-logo.png';

export type BrandLogoProps = {
  /** Collapsed sidebar mode: render a smaller version of the brand image. */
  markOnly?: boolean;
  /** Height of the brand image in pixels (expanded mode default 30px). */
  markSize?: number;
  className?: string;
  /** Kept for backward compatibility; no longer used (the brand image already contains the wordmark). */
  wordmarkClassName?: string;
};

/** Fosun Holiday (复星旅文) brand lockup — single horizontal image, transparent background. */
export default function BrandLogo({
  markOnly = false,
  markSize,
  className,
}: BrandLogoProps) {
  // 原图 186x60 (3.1:1)，按高度驱动宽度自适应；折叠栏内宽约 40px，缩到 14px 高防溢出
  const height = markSize ?? (markOnly ? 14 : 30);
  return (
    <span
      className={cn('flex items-center overflow-hidden p-[2px]', className)}
      style={{ height: height + 4 }}
    >
      <img
        src={logoFull}
        alt="Fosun Holiday 复星旅文"
        className="shrink-0"
        style={{ height, width: Math.round(height * 3.1) }}
      />
    </span>
  );
}
