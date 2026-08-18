import { describe, expect, it } from 'vitest';

import {
  EMPLOYEE_AVATAR_PRESETS,
  EMPLOYEE_TEMPLATES,
  employeeAvatarImage,
  employeeMetadataFromTemplate,
} from './employee';

const EXPANDED_EMPLOYEES = [
  ['sales-advisor', 'sales-handshake', '销售', '客户拓展顾问', 'staffdeck-avatar-sales.png'],
  ['marketing-planner', 'marketing-spark', '市场', '市场内容策划', 'staffdeck-avatar-marketing.png'],
  ['procurement-coordinator', 'procurement-check', '采购', '采购协同专员', 'staffdeck-avatar-procurement.png'],
  ['project-manager', 'project-board', '项目管理', '项目推进经理', 'staffdeck-avatar-project.png'],
  ['data-analyst', 'data-insight', '数据分析', '经营分析师', 'staffdeck-avatar-data.png'],
] as const;

describe('expanded employee presets', () => {
  it.each(EXPANDED_EMPLOYEES)(
    'registers %s with its own avatar and template metadata',
    (roleKey, avatarPreset, categoryName, roleName, avatarFilename) => {
      const preset = EMPLOYEE_AVATAR_PRESETS.find((item) => item.key === avatarPreset);
      const template = EMPLOYEE_TEMPLATES.find((item) => item.key === roleKey);
      const metadata = employeeMetadataFromTemplate(roleKey);
      const avatarImage = employeeAvatarImage({
        avatarKind: 'preset',
        avatarImage: '',
        avatarPreset,
      });

      expect(preset?.label).toContain(categoryName);
      expect(template).toMatchObject({ roleName, avatarPreset });
      expect(metadata).toMatchObject({
        role_key: roleKey,
        role_name: roleName,
        avatar_kind: 'preset',
        avatar_preset: avatarPreset,
      });
      expect(avatarImage).toContain(avatarFilename);
    },
  );

  it('uses a distinct illustration for each expanded employee', () => {
    const images = EXPANDED_EMPLOYEES.map(([, avatarPreset]) => employeeAvatarImage({
      avatarKind: 'preset',
      avatarImage: '',
      avatarPreset,
    }));

    expect(new Set(images)).toHaveLength(EXPANDED_EMPLOYEES.length);
  });
});
