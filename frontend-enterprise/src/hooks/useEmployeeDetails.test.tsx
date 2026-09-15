// @vitest-environment jsdom
import { renderHook, waitFor, cleanup } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { useEmployeeDetails } from './useEmployeeDetails';
import { api } from '../api/client';
import type { AgentProfileRead } from '../types';
vi.mock('../api/client',()=>({api:{get:vi.fn()}}));
afterEach(()=>{cleanup();vi.clearAllMocks();});
it('loads full profile only when a summary is opened for editing',async()=>{
  const summary: AgentProfileRead={id:'a',tenant_id:'t',name:'A',is_overall:false,status:'active',resources:[],
    created_at:'2026-09-14',updated_at:'2026-09-14',metadata:{directory_summary:true}};
  const full={...summary,persona_prompt:'keep persona',metadata:{avatar_image:'data:image/png;base64,AA=='}};
  vi.mocked(api.get).mockResolvedValue(full);
  const {result,rerender}=renderHook(({open})=>useEmployeeDetails(summary,open),{initialProps:{open:false}});
  expect(api.get).not.toHaveBeenCalled();expect(result.current).toBeNull();
  rerender({open:true});await waitFor(()=>expect(result.current).toEqual(full));
  expect(api.get).toHaveBeenCalledTimes(1);
});
