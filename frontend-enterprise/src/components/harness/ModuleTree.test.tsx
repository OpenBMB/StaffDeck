// @vitest-environment jsdom
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import { afterEach, describe, it, expect, vi } from 'vitest';
import ModuleTree, { desiredState, moduleSelectionPatch, missingModuleDependencies } from './ModuleTree';
import type { HarnessAssembly, HarnessModule, HarnessTreeBig } from '@/api/harness';

const assembly: HarnessAssembly = { engine: 'harness_v3', security_profile: 'OSS_LOCAL', selections: { 'source.staff': 'local.staff', 'source.sop': 'local.sop' },
  disabled_modules: ['remote.staff', 'security.business_base'], enabled_modules: [], extra_modules: [], placements: {}, base: {} as HarnessAssembly['base'] };
const remote = { module_id: 'remote.staff', slot: 'source.staff', name: '企业员工来源', enabled: false, kind: 'A',
  requires: [], provides: [], hooks: [], policy_actions: [], metadata: {}, source: 'builtin' } as unknown as HarnessModule;
const tree = [{ id: 'staff', name: '数字员工', description: '', total: 1, enabled: 0, edges: [], subs: [{ id: 'source', name: '员工来源',
  description: '', kind: 'A', total: 1, enabled: 0, modules: [remote], legacy: [] }] }] as unknown as HarnessTreeBig[];
afterEach(cleanup);

describe('module assembly in the tree', () => {
  it('可选委托支持不启用，不改变用户的渠道或其他插槽选择', () => {
    const m = { ...remote, module_id: 'business.execution', slot: 'runtime.execution', optional_slot: true };
    const before = { ...assembly, selections: { ...assembly.selections, 'runtime.execution': m.module_id }, enabled_modules: ['channel.feishu'] };
    const patch = moduleSelectionPatch(m, before, false);
    expect(patch.selections).toEqual(assembly.selections);
    expect(patch.disabled_modules).toContain(m.module_id);
    expect(patch.enabled_modules).toEqual(['channel.feishu']);
    expect(() => moduleSelectionPatch(remote, assembly, false)).toThrow('Required slots');
  });
  it('停用可选接入点不能悄悄恢复另一个默认实现', () => {
    const m = { ...remote, module_id: 'business.workspace', slot: 'runtime.workspace', optional_slot: true };
    const local = { ...m, module_id: 'workspace.local', metadata: { default_enabled: true } };
    const patch = moduleSelectionPatch(m, assembly, false, [m, local]);
    const after = { ...assembly, selections: patch.selections, disabled_modules: patch.disabled_modules! };
    expect(desiredState(local, after).enabled).toBe(false);
    expect(desiredState(m, after).enabled).toBe(false);
  });
  it('本地运行数据搭配企业委托立即显示缺少的实际模块，并能在树内取消', () => {
    const execution = { ...remote, module_id: 'business.execution', slot: 'runtime.execution', name: 'Base 执行委托与生命周期', optional_slot: true, feature_requires: ['storage.business.runtime'] };
    const storage = { ...remote, module_id: 'business.runtime', slot: 'runtime.services', name: '企业运行数据接线', feature_exports: ['storage.business.runtime'] };
    const current = { ...assembly, selections: { ...assembly.selections, 'runtime.execution': execution.module_id, 'runtime.services': 'runtime.services.local' } };
    expect(missingModuleDependencies(execution, [execution, storage], current)).toEqual(['企业运行数据接线']);
    const mixedTree = [{ ...tree[0], subs: [{ ...tree[0].subs[0], modules: [execution, storage] }] }];
    const choose = vi.fn();
    render(<ModuleTree tree={mixedTree} tech={false} assembly={current} options={[]} onToggleModule={vi.fn()} onChooseModule={choose} onPlaceModule={vi.fn()} />);
    expect(screen.getByRole('alert').textContent).toContain('企业运行数据接线');
    fireEvent.change(screen.getByPlaceholderText('搜索模块或能力，例如“知识”“飞书”'), { target: { value: 'business.execution' } });
    fireEvent.click(screen.getByRole('button', { name: '停用 Base 执行委托与生命周期' }));
    expect(choose).toHaveBeenCalledWith(execution, false);
  });
  it('单模块选择保留其他插槽并解除被选模块的停用标记', () => {
    expect(moduleSelectionPatch(remote, assembly)).toEqual({ selections: { 'source.staff': 'remote.staff', 'source.sop': 'local.sop' }, disabled_modules: ['security.business_base'] });
  });
  it('权限与引擎通过同一入口同步选择字段，不留下相互冲突的旧选择', () => {
    const pep = { ...remote, slot: 'security.pep', module_id: 'security.business_base' };
    const patch = moduleSelectionPatch(pep, assembly);
    expect(patch.security_profile).toBe('BUSINESS_BASE');
    expect(patch.selections?.['security.pep']).toBe('security.business_base');
    expect(desiredState(pep, { ...assembly, selections: patch.selections, disabled_modules: patch.disabled_modules! })).toMatchObject({ enabled: true, kind: 'choice' });
    expect(moduleSelectionPatch({ ...remote, slot: 'runtime.engine', module_id: 'engine.harness_v3' }, assembly)).toMatchObject({ engine: 'harness_v3', selections: { 'runtime.engine': 'engine.harness_v3' } });
  });
  it('在模块内部可直接选择实现，不再要求到上方插槽表单操作', () => {
    const choose = vi.fn();
    render(<ModuleTree tree={tree} tech={false} assembly={assembly} options={[]} onToggleModule={vi.fn()} onChooseModule={choose} onPlaceModule={vi.fn()} />);
    fireEvent.change(screen.getByPlaceholderText('搜索模块或能力，例如“知识”“飞书”'), { target: { value: '企业员工' } });
    fireEvent.click(screen.getByRole('button', { name: '使用 企业员工来源' }));
    expect(choose).toHaveBeenCalledWith(remote);
    expect(screen.queryByText('由插槽选择')).toBeNull();
  });
});
