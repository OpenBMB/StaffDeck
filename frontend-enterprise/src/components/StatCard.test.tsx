import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import { StatCard } from './StatCard';

describe('StatCard', () => {
  it('aligns values and labels on their text baseline', () => {
    const rendered = renderToStaticMarkup(
      createElement(StatCard, { value: 'GLM-5.2', label: '默认模型' }),
    );

    expect(rendered).toContain('items-baseline');
    expect(rendered).not.toContain('items-end');
    expect(rendered).toContain('>GLM-5.2<');
    expect(rendered).toContain('>默认模型<');
  });
});
