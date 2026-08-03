// @vitest-environment jsdom

import { createElement } from 'react';
import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { I18nProvider } from '@/i18n';

import AppHeader from './AppHeader';

describe('AppHeader', () => {
  it('allows simple page titles to opt into centered controls', () => {
    const { container } = render(
      createElement(
        I18nProvider,
        null,
        createElement(AppHeader, {
          title: '模型',
          right: createElement('span', null, 'controls'),
          className: 'items-center',
        }),
      ),
    );
    const header = container.querySelector('header');

    expect(header?.classList.contains('items-center')).toBe(true);
    expect(header?.classList.contains('items-start')).toBe(false);
  });
});
