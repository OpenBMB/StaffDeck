/**
 * 技能广场「标签分类」的唯一口径。
 *
 * 技能管理页（GeneralSkillsPage：列表过滤 + 新建/编辑表单）与开放广场
 * （OpenPlatformPage：`/enterprise/platform/general-skills` 的下拉过滤）共用这一份，
 * 避免两处各自维护导致选项漂移。
 */
export const GENERAL_SKILL_CATEGORIES = ['办公类', '财务类', '人力资源类', '编程类'] as const;

/** Radix Select 不接受空串 value，用哨兵值表示「未分类」（表单里可显式清空）。 */
export const GENERAL_SKILL_CATEGORY_NONE = '__none__';

/** 下拉过滤「全部分类」的哨兵值；与具体分类值（中文）区分开。 */
export const GENERAL_SKILL_CATEGORY_ALL = 'all';