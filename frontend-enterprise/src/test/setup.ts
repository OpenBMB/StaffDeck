const hasUsableLocalStorage = typeof window !== 'undefined'
  && typeof window.localStorage?.getItem === 'function'
  && typeof window.localStorage?.setItem === 'function';

if (typeof window !== 'undefined' && !hasUsableLocalStorage) {
  const values = new Map<string, string>();
  const storage: Storage = {
    get length() {
      return values.size;
    },
    clear() {
      values.clear();
    },
    getItem(key) {
      return values.get(key) ?? null;
    },
    key(index) {
      return [...values.keys()][index] ?? null;
    },
    removeItem(key) {
      values.delete(key);
    },
    setItem(key, value) {
      values.set(key, String(value));
    },
  };
  Object.defineProperty(window, 'localStorage', { configurable: true, value: storage });
}
