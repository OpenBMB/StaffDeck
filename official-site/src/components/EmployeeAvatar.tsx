import type { CSSProperties } from 'react';

import {
  DEFAULT_AVATAR_PRESET,
  employeeAvatarImage,
  employeeProfile,
  type EmployeeProfile,
} from '../employee';
import type { AgentProfileRead } from '../types';

type AvatarProfile = Pick<EmployeeProfile, 'avatarKind' | 'avatarImage' | 'avatarPreset' | 'avatarText' | 'avatarTone'>;

type EmployeeAvatarProps = {
  agent?: AgentProfileRead | null;
  /** Pre-resolved profile. When omitted it is derived from `agent`. */
  profile?: AvatarProfile;
  /** Square shorthand for width/height (px). Used when width/height are not provided. */
  size?: number;
  /** Explicit width in px. Falls back to `size`. */
  width?: number;
  /** Explicit height in px. Falls back to `size`. */
  height?: number;
  /** Border radius override (px or any CSS length). */
  radius?: number | string;
  /** How the image fills the box. `cover` fills the frame without distortion. */
  fit?: CSSProperties['objectFit'];
  /** Alignment of the image within the box, e.g. `center bottom`. */
  objectPosition?: CSSProperties['objectPosition'];
  className?: string;
  style?: CSSProperties;
};

export default function EmployeeAvatar({
  agent,
  profile: profileOverride,
  size = 54,
  width,
  height,
  radius,
  fit = 'cover',
  objectPosition = 'center',
  className = '',
  style,
}: EmployeeAvatarProps) {
  const profile = profileOverride || employeeProfile(agent);

  const className_ = [
    'employee-avatar',
    className,
  ]
    .filter(Boolean)
    .join(' ');

  const boxStyle: CSSProperties = {
    width: width ?? size,
    height: height ?? size,
    ...(radius != null ? { borderRadius: radius } : null),
    ...style,
  };

  // Lock the image to the box at any width/height: `cover` fills without distortion,
  // while resetting transform/max-width neutralizes per-context overrides.
  const imageStyle: CSSProperties = {
    width: '100%',
    height: '100%',
    maxWidth: 'none',
    objectFit: fit,
    objectPosition,
    transform: 'none'
  };

  return (
    <span
      className={className_}
      style={boxStyle}
      aria-label={`${profile.avatarText || '员'}员工头像`}
    >
      <img src={employeeAvatarImage(profile)} alt="" style={imageStyle} />
    </span>
  );
}
