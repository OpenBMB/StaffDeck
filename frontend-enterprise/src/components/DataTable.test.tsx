// @vitest-environment jsdom

import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { DataTable, type DataTableColumn } from './DataTable';

type Row = {
  id: string;
  name: string;
};

describe('DataTable', () => {
  it('keeps a sticky action column visible in a horizontally scrollable table', () => {
    const columns: DataTableColumn<Row>[] = [
      { key: 'name', title: '名称', dataIndex: 'name', width: 320 },
      {
        key: 'actions',
        title: '操作',
        width: 80,
        sticky: 'right',
        render: () => <button type="button">编辑</button>,
      },
    ];

    render(
      <DataTable
        aria-label="SOP 列表"
        columns={columns}
        data={[{ id: 'sop-1', name: 'Fault report' }]}
        rowKey={(row) => row.id}
      />,
    );

    const table = screen.getByRole('table', { name: 'SOP 列表' });
    const actionHeader = screen.getByRole('columnheader', { name: '操作' });
    const actionCell = screen.getByRole('button', { name: '编辑' }).closest('td');

    expect(table.style.minWidth).toBe('400px');
    expect(actionHeader.className).toContain('sticky');
    expect(actionHeader.className).toContain('right-0');
    expect(actionCell?.className).toContain('sticky');
    expect(actionCell?.className).toContain('right-0');
    expect(actionCell?.className).toContain('bg-white');
    expect(screen.getByRole('columnheader', { name: '名称' }).className).not.toContain('sticky');
  });
});
