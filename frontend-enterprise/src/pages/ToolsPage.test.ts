import { describe, expect, it } from 'vitest';

import { parseMcpArgs } from './ToolsPage';

describe('parseMcpArgs', () => {
  it('preserves spaces inside one argument', () => {
    expect(parseMcpArgs('C:\\Program Files\\mcp server\\index.js')).toEqual([
      'C:\\Program Files\\mcp server\\index.js',
    ]);
  });

  it('uses one non-empty line per argument', () => {
    expect(parseMcpArgs('-m\nmy_mcp.server\n\n--label=customer support')).toEqual([
      '-m',
      'my_mcp.server',
      '--label=customer support',
    ]);
  });
});
