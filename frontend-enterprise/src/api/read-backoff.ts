/** A per-endpoint read circuit. It never retries or blocks writes. */
export class ReadBackoff {
  private states = new Map<string, { failures: number; until: number; error: unknown }>();

  blocked(path: string, now = Date.now()): unknown {
    const state = this.states.get(path);
    return state && state.until > now ? state.error : null;
  }

  failed(path: string, error: unknown, now = Date.now()): void {
    const failures = (this.states.get(path)?.failures || 0) + 1;
    const delay = Math.min(10000, 1000 * 2 ** Math.min(failures - 1, 4));
    if (!this.states.has(path) && this.states.size >= 128) this.states.delete(this.states.keys().next().value!);
    this.states.set(path, { failures, until: now + delay, error });
  }

  succeeded(path: string): void { this.states.delete(path); }
}
