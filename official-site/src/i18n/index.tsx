import {
  createContext,
  useCallback,
  useContext,
  useLayoutEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';

import englishCatalog from './en.json';

export type AppLocale = 'zh-CN' | 'en-US';

type I18nContextValue = {
  locale: AppLocale;
  setLocale: (locale: AppLocale) => void;
  toggleLocale: () => void;
  t: (source: string, values?: Record<string | number, string | number>) => string;
};

type CompiledTemplate = {
  pattern: RegExp;
  placeholderOrder: number[];
  target: string;
};

const STORAGE_KEY = 'staffdeck_locale';
const CATALOG = englishCatalog as Record<string, string>;
const SAFE_ATTRIBUTES = ['placeholder', 'aria-label', 'title', 'alt', 'data-placeholder'] as const;
const TEMPLATE_TOKEN = /\{(\d+)\}/g;
const textOriginals = new WeakMap<Text, string>();
const attributeOriginals = new WeakMap<Element, Map<string, string>>();
const translationCache = new Map<string, string | null>();

function initialLocale(): AppLocale {
  if (typeof window === 'undefined') return 'zh-CN';
  return window.localStorage.getItem(STORAGE_KEY) === 'en-US' ? 'en-US' : 'zh-CN';
}

let currentLocale: AppLocale = initialLocale();

function decodeSourceEntities(value: string): string {
  return value
    .replace(/&gt;/g, '>')
    .replace(/&lt;/g, '<')
    .replace(/&amp;/g, '&')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'");
}

function escapePattern(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

const exactCatalog = new Map<string, string>();
const compiledTemplates: CompiledTemplate[] = [];

for (const [rawSource, target] of Object.entries(CATALOG)) {
  const sources = new Set([rawSource, decodeSourceEntities(rawSource)]);
  for (const source of sources) {
    if (!source) continue;
    exactCatalog.set(source, target);
    if (!TEMPLATE_TOKEN.test(source)) {
      TEMPLATE_TOKEN.lastIndex = 0;
      continue;
    }
    TEMPLATE_TOKEN.lastIndex = 0;
    const placeholderOrder: number[] = [];
    let cursor = 0;
    let pattern = '^';
    for (const match of source.matchAll(TEMPLATE_TOKEN)) {
      pattern += escapePattern(source.slice(cursor, match.index));
      pattern += '([\\s\\S]*?)';
      placeholderOrder.push(Number(match[1]));
      cursor = (match.index || 0) + match[0].length;
    }
    pattern += `${escapePattern(source.slice(cursor))}$`;
    compiledTemplates.push({ pattern: new RegExp(pattern), placeholderOrder, target });
  }
}

compiledTemplates.sort((left, right) => right.pattern.source.length - left.pattern.source.length);

function interpolate(target: string, values: Record<string | number, string | number>): string {
  return target.replace(TEMPLATE_TOKEN, (_, key: string) => String(values[key] ?? `{${key}}`));
}

function translateCore(source: string): string | null {
  if (translationCache.has(source)) return translationCache.get(source) || null;
  const exact = exactCatalog.get(source);
  if (exact) {
    translationCache.set(source, exact);
    return exact;
  }
  for (const template of compiledTemplates) {
    const match = source.match(template.pattern);
    if (!match) continue;
    const values: Record<number, string> = {};
    template.placeholderOrder.forEach((placeholder, index) => {
      values[placeholder] = match[index + 1] || '';
    });
    const translated = interpolate(template.target, values);
    translationCache.set(source, translated);
    return translated;
  }
  translationCache.set(source, null);
  return null;
}

function splitWhitespace(value: string): { leading: string; core: string; trailing: string } {
  const leading = value.match(/^\s*/)?.[0] || '';
  const trailing = value.match(/\s*$/)?.[0] || '';
  return {
    leading,
    core: value.slice(leading.length, value.length - trailing.length),
    trailing,
  };
}

function translatePreservingWhitespace(value: string): string {
  const { leading, core, trailing } = splitWhitespace(value);
  if (!core) return value;
  const translated = translateCore(core);
  return translated ? `${leading}${translated}${trailing}` : value;
}

function shouldIgnore(element: Element | null): boolean {
  return Boolean(
    element?.closest(
      '[data-i18n-ignore], code, pre, script, style, textarea, [contenteditable="true"]',
    ),
  );
}

function shouldIgnoreAttribute(element: Element): boolean {
  return Boolean(element.closest('[data-i18n-ignore], code, pre, script, style'));
}

function localizeTextNode(node: Text, locale: AppLocale): void {
  if (shouldIgnore(node.parentElement)) return;
  const current = node.data;
  const previousSource = textOriginals.get(node);
  const previousTarget = previousSource ? translatePreservingWhitespace(previousSource) : '';

  if (locale === 'zh-CN') {
    if (previousSource && current === previousTarget && current !== previousSource) {
      node.data = previousSource;
    } else if (!previousSource || (current !== previousSource && current !== previousTarget)) {
      textOriginals.set(node, current);
    }
    return;
  }

  let source = previousSource;
  if (!source || (current !== source && current !== previousTarget)) {
    source = current;
    textOriginals.set(node, source);
  }
  const target = translatePreservingWhitespace(source);
  if (target !== current) node.data = target;
}

function localizeAttribute(element: Element, name: string, locale: AppLocale): void {
  if (shouldIgnoreAttribute(element)) return;
  const current = element.getAttribute(name);
  if (current == null) return;
  let originals = attributeOriginals.get(element);
  if (!originals) {
    originals = new Map();
    attributeOriginals.set(element, originals);
  }
  const previousSource = originals.get(name);
  const previousTarget = previousSource ? translatePreservingWhitespace(previousSource) : '';

  if (locale === 'zh-CN') {
    if (previousSource && current === previousTarget && current !== previousSource) {
      element.setAttribute(name, previousSource);
    } else if (!previousSource || (current !== previousSource && current !== previousTarget)) {
      originals.set(name, current);
    }
    return;
  }

  let source = previousSource;
  if (!source || (current !== source && current !== previousTarget)) {
    source = current;
    originals.set(name, source);
  }
  const target = translatePreservingWhitespace(source);
  if (target !== current) element.setAttribute(name, target);
}

function localizeElement(element: Element, locale: AppLocale): void {
  for (const name of SAFE_ATTRIBUTES) localizeAttribute(element, name, locale);
}

function localizeSubtree(root: Node, locale: AppLocale): void {
  if (root.nodeType === Node.TEXT_NODE) {
    localizeTextNode(root as Text, locale);
    return;
  }
  if (root.nodeType !== Node.ELEMENT_NODE && root.nodeType !== Node.DOCUMENT_NODE) return;
  if (root.nodeType === Node.ELEMENT_NODE) localizeElement(root as Element, locale);
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
  let current = walker.nextNode();
  while (current) {
    if (current.nodeType === Node.TEXT_NODE) localizeTextNode(current as Text, locale);
    else localizeElement(current as Element, locale);
    current = walker.nextNode();
  }
}

const I18nContext = createContext<I18nContextValue | null>(null);

export function getStoredLocale(): AppLocale {
  return currentLocale;
}

export function getDateLocale(): string {
  return currentLocale === 'en-US' ? 'en-US' : 'zh-CN';
}

export function I18nProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<AppLocale>(initialLocale);

  const setLocale = useCallback((nextLocale: AppLocale) => {
    currentLocale = nextLocale;
    window.localStorage.setItem(STORAGE_KEY, nextLocale);
    document.documentElement.lang = nextLocale;
    setLocaleState(nextLocale);
  }, []);

  const toggleLocale = useCallback(() => {
    setLocale(locale === 'zh-CN' ? 'en-US' : 'zh-CN');
  }, [locale, setLocale]);

  const t = useCallback(
    (source: string, values: Record<string | number, string | number> = {}) => {
      if (locale === 'zh-CN') return interpolate(source, values);
      return interpolate(translateCore(source) || source, values);
    },
    [locale],
  );

  useLayoutEffect(() => {
    currentLocale = locale;
    document.documentElement.lang = locale;
    const root = document.documentElement;
    localizeSubtree(root, locale);

    let scheduled = false;
    let disposed = false;
    const pending = new Set<Node>();
    const flush = () => {
      scheduled = false;
      if (disposed) return;
      for (const node of pending) localizeSubtree(node, locale);
      pending.clear();
    };
    const enqueue = (node: Node) => {
      pending.add(node);
      if (scheduled) return;
      scheduled = true;
      window.queueMicrotask(flush);
    };
    const observer = new MutationObserver((mutations) => {
      for (const mutation of mutations) {
        if (mutation.type === 'characterData') enqueue(mutation.target);
        else if (mutation.type === 'attributes') enqueue(mutation.target);
        else mutation.addedNodes.forEach(enqueue);
      }
    });
    observer.observe(root, {
      subtree: true,
      childList: true,
      characterData: true,
      attributes: true,
      attributeFilter: [...SAFE_ATTRIBUTES],
    });
    return () => {
      disposed = true;
      observer.disconnect();
      pending.clear();
    };
  }, [locale]);

  const value = useMemo<I18nContextValue>(
    () => ({ locale, setLocale, toggleLocale, t }),
    [locale, setLocale, t, toggleLocale],
  );

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nContextValue {
  const context = useContext(I18nContext);
  if (!context) throw new Error('useI18n must be used inside I18nProvider');
  return context;
}
