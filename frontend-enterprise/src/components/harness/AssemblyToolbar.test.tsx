// @vitest-environment jsdom
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { describe, it, expect, vi, afterEach } from 'vitest';
import AssemblyToolbar from './AssemblyToolbar';
import type { HarnessAssembly, HarnessAssemblyState } from '@/api/harness';
import { harnessApi } from '@/api/harness';

vi.mock('@/api/harness', () => ({ harnessApi: {
  assemblyOptions: vi.fn().mockResolvedValue({ presets: [{ id: 'business', name: '商业装配', description: '测试预设', assembly: { selections: { 'source.staff': 'remote.staff' } } }], slots: [
    { slot: 'source.staff', active: 'local.staff', choices: [
      { module_id: 'local.staff', name: '本地员工', requires: [], exports: [] },
      { module_id: 'remote.staff', name: '企业员工', requires: ['identity.base'], exports: [] },
    ] },
  ] }),
  assemblyPreview: vi.fn().mockResolvedValue({ ok: false, error: '缺少身份模块' }),
  savePreset: vi.fn().mockResolvedValue({ id: 'custom-test', name: '我的装配', description: '', custom: true, assembly: { selections: { 'source.sop': 'local.sop' } } }),
} }));

const assembly: HarnessAssembly = { engine: 'harness_v3', security_profile: 'OSS_LOCAL',
  selections: { 'source.sop': 'local.sop' }, placements: {}, extra_modules: [], disabled_modules: [],
  base: {} as HarnessAssembly['base'] };
const state: HarnessAssemblyState = { saved: assembly, applied: assembly, restart_count: 0,
  pending: false, started_at: null, last_restart_error: null, config_path: 'test-config.json' };

afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe('AssemblyToolbar', () => {
  it('保存当前配置为独立预设，刷新后可载入且不自动应用', async () => {
    const save = vi.fn().mockResolvedValue(true);
    render(<AssemblyToolbar tenantId="test" state={state} busy={false} onSave={save} />);
    await screen.findByText('商业装配');
    fireEvent.click(screen.getByText('保存为预设'));
    fireEvent.change(screen.getByLabelText('预设名称'), { target: { value: '我的装配' } });
    fireEvent.change(screen.getByLabelText('保存来源'), { target: { value: 'applied' } });
    fireEvent.click(screen.getByText('保存预设'));
    await screen.findByText('我的装配');
    expect(harnessApi.savePreset).toHaveBeenCalledWith('test', { name: '我的装配', description: '', source: 'applied' });
    expect(save).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText('载入为待应用清单'));
    expect(save).toHaveBeenCalledWith({ selections: { 'source.sop': 'local.sop' } });
  });
  it('保存失败保留输入，不隐藏错误或改动装配', async () => {
    vi.mocked(harnessApi.savePreset).mockRejectedValueOnce(new Error('同名预设已存在，请使用其他名称'));
    const save = vi.fn();
    render(<AssemblyToolbar tenantId="test" state={state} busy={false} onSave={save} />);
    fireEvent.click(screen.getByText('保存为预设'));
    fireEvent.change(screen.getByLabelText('预设名称'), { target: { value: '已有装配' } });
    fireEvent.click(screen.getByText('保存预设'));
    expect((await screen.findByRole('alert')).textContent).toContain('同名预设');
    expect((screen.getByLabelText('预设名称') as HTMLInputElement).value).toBe('已有装配');
    expect(save).not.toHaveBeenCalled();
  });
  it('只保留预设工具栏，选择预设不自动保存，载入后才填写待应用配置', async () => {
    const save = vi.fn().mockResolvedValue(true);
    render(<AssemblyToolbar tenantId="test" state={state} busy={false} onSave={save} />);
    fireEvent.change(await screen.findByLabelText('装配预设'), { target: { value: 'business' } });
    expect(save).not.toHaveBeenCalled();
    expect(screen.queryByLabelText('员工来源')).toBeNull();
    expect(screen.queryByText('按插槽装配')).toBeNull();
    fireEvent.click(screen.getByText('载入为待应用清单'));
    expect(save).toHaveBeenCalledWith({ selections: { 'source.staff': 'remote.staff' } });
  });
  it('展示预检查拒绝，不自动保存或应用', async () => {
    const save = vi.fn().mockResolvedValue(true);
    render(<AssemblyToolbar tenantId="test" state={state} busy={false} onSave={save} />);
    fireEvent.click(screen.getByText('检查待应用装配'));
    await waitFor(() => expect(screen.getByRole('status').textContent).toContain('缺少身份模块'));
    expect(save).not.toHaveBeenCalled();
  });
});
