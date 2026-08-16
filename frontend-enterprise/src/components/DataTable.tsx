import type { ReactNode } from 'react';

import { cn } from '@/lib/utils';

import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from './ui';

export type DataTableColumn<T> = {
  /** Unique column key. */
  key: string;
  /** Header cell content. */
  title: ReactNode;
  /** Cell renderer. Falls back to `row[dataIndex]` when omitted. */
  render?: (row: T, index: number) => ReactNode;
  /** Shortcut for reading a plain field value when no `render` is provided. */
  dataIndex?: keyof T;
  /** Fixed column width (px number or any CSS width). */
  width?: number | string;
  align?: 'left' | 'center' | 'right';
  /** Extra classes for the body cell. */
  className?: string;
  /** Extra classes for the header cell. */
  headClassName?: string;
  /** Keep the column visible at an edge while the table scrolls horizontally. */
  sticky?: 'left' | 'right';
};

export type DataTableProps<T> = {
  columns: DataTableColumn<T>[];
  data: T[];
  rowKey: (row: T, index: number) => string | number;
  loading?: boolean;
  emptyText?: ReactNode;
  loadingText?: ReactNode;
  onRowClick?: (row: T, index: number) => void;
  /** Body row height. `default` = 64px, `compact` = 46px (SD1 execution-log style). */
  size?: 'default' | 'compact';
  /** Zebra striping: even rows get a subtle `#fbfbfb` fill. */
  striped?: boolean;
  /** Full grid: every cell is bordered instead of row-only dividers. */
  bordered?: boolean;
  /** Extra classes for the outer rounded container. */
  className?: string;
  'aria-label'?: string;
};

const ALIGN_CLASS = {
  left: 'text-left',
  center: 'text-center',
  right: 'text-right',
} as const;

const HEAD_CELL_CLASS =
  'h-[36px] bg-[#f2f3f7] px-[16px] py-[12px] align-middle text-[12px] font-normal text-[#464c5e]';
const BODY_CELL_CLASS = 'px-[16px] py-[12px] align-middle text-[12px] text-[#858b9c]';
const BODY_HEIGHT = {
  default: 'min-h-[64px]',
  compact: 'min-h-[46px]',
} as const;
const CELL_BORDER = 'border border-[#f2f3f7]';
const STICKY_HEAD_CLASS = {
  left: 'sticky left-0 z-20 border-r border-[#e3e6ed] bg-[#f2f3f7]',
  right: 'sticky right-0 z-20 border-l border-[#e3e6ed] bg-[#f2f3f7]',
} as const;
const STICKY_BODY_CLASS = {
  left: 'sticky left-0 z-10 border-r border-[#e3e6ed]',
  right: 'sticky right-0 z-10 border-l border-[#e3e6ed]',
} as const;

/**
 * Business data table (SD1 designs: node 281:1942 default, 281:2040 compact grid).
 * Rounded `#f2f3f7` frame, gray header row, white body rows with hairline dividers.
 * Built on top of the shadcn `Table` primitives but owns the product-specific styling.
 */
export function DataTable<T>({
  columns,
  data,
  rowKey,
  loading = false,
  emptyText = '暂无数据',
  loadingText = '加载中…',
  onRowClick,
  size = 'default',
  striped = false,
  bordered = false,
  className,
  'aria-label': ariaLabel,
}: DataTableProps<T>) {
  const hasData = data.length > 0;
  const fixedTableWidth = columns.every((column) => typeof column.width === 'number')
    ? columns.reduce((total, column) => total + (column.width as number), 0)
    : undefined;

  return (
    <div
      className={cn(
        'overflow-hidden rounded-[14px] border border-[#f2f3f7]',
        className,
      )}
    >
      <Table
        className="w-full table-fixed text-[12px]"
        style={fixedTableWidth ? { minWidth: fixedTableWidth } : undefined}
        aria-label={ariaLabel}
      >
        <TableHeader>
          <TableRow className="border-0 hover:bg-transparent">
            {columns.map((column) => (
              <TableHead
                key={column.key}
                style={column.width ? { width: column.width } : undefined}
                className={cn(
                  HEAD_CELL_CLASS,
                  bordered && CELL_BORDER,
                  ALIGN_CLASS[column.align ?? 'left'],
                  column.sticky && STICKY_HEAD_CLASS[column.sticky],
                  column.headClassName,
                )}
              >
                {column.title}
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {hasData ? (
            data.map((row, index) => (
              <TableRow
                key={rowKey(row, index)}
                onClick={onRowClick ? () => onRowClick(row, index) : undefined}
                className={cn(
                  'group has-aria-expanded:bg-transparent',
                  bordered
                    ? 'border-0'
                    : 'border-b border-[#f2f3f7] last:border-0',
                  striped
                    ? index % 2 === 1
                      ? 'bg-[#fbfbfb] hover:bg-[#f2f3f7]'
                      : 'bg-white hover:bg-[#f2f3f7]'
                    : 'hover:bg-[#fafbfc]',
                  onRowClick && 'cursor-pointer',
                )}
              >
                {columns.map((column) => (
                  <TableCell
                    key={column.key}
                    className={cn(
                      BODY_CELL_CLASS,
                      BODY_HEIGHT[size],
                      bordered && CELL_BORDER,
                      ALIGN_CLASS[column.align ?? 'left'],
                      column.sticky && STICKY_BODY_CLASS[column.sticky],
                      column.sticky &&
                        (striped && index % 2 === 1
                          ? 'bg-[#fbfbfb] group-hover:bg-[#f2f3f7]'
                          : 'bg-white group-hover:bg-[#fafbfc]'),
                      column.className,
                    )}
                  >
                    {column.render
                      ? column.render(row, index)
                      : column.dataIndex != null
                        ? (row[column.dataIndex] as ReactNode)
                        : null}
                  </TableCell>
                ))}
              </TableRow>
            ))
          ) : (
            <TableRow className="hover:bg-transparent">
              <TableCell colSpan={columns.length} className="h-[160px] text-center align-middle text-[13px] text-[#858b9c]">
                {loading ? loadingText : emptyText}
              </TableCell>
            </TableRow>
          )}
        </TableBody>
      </Table>
    </div>
  );
}
