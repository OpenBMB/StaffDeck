// @vitest-environment jsdom
import { Fragment, useEffect, useRef } from 'react'
import { Context } from '@deepseek-ai/cordis'
import { describe, expect, it, vi } from 'vitest'
import { act, render } from '@testing-library/react'
import type {
  SessionProviderComponent, StoredEntry,
} from '@deepseek-ai/dsh-client-ui-slots'
import type {
  ScopedStandardSourceBinding, SlotRendererHost, SlotScopeAdapter, StandardSourceBinding,
} from '@deepseek-ai/dsh-client-ui-renderer/client'
import { createSlotRenderer } from '../src/client/scoped-slots.tsx'

type SessionBinding = ScopedStandardSourceBinding

function observable<T>(initial: T) {
  let value = initial
  const subs = new Set<() => void>()
  return {
    getSnapshot: () => value,
    subscribe: (fn: () => void) => { subs.add(fn); return () => { subs.delete(fn) } },
    set: (next: T) => { value = next; for (const fn of [...subs]) fn() },
  }
}

/**
 * Minimal host: SessionProvider only reads the Session scope adapter, but it must
 * render inside the renderer tree (HostContext), so the harness mounts a real
 * root entry whose body is the test's render-prop provider.
 */
function makeHost(
  bodies: {
    root: (
      rp: (key: string, owner: object) => React.ReactNode,
      SessionProvider: SessionProviderComponent,
    ) => React.ReactNode
  },
  options: { installRenderArea?: boolean } = {},
) {
  const scopeCtx = new Context()
  const absentBinding: StandardSourceBinding = {
    key: undefined,
    hooks: { session: undefined },
    keyedHooks: {},
    props: { sessionId: undefined },
  }
  const currentBinding = observable<StandardSourceBinding>(absentBinding)
  let currentId: string | undefined
  const bindings = new Map<string, SessionBinding>()
  const sessionEntries: StoredEntry[] = []
  const root = observable<StandardSourceBinding>({
    key: undefined,
    hooks: {},
    keyedHooks: {},
    props: {},
  })
  const sessionAdapter: SlotScopeAdapter = {
    current: currentBinding,
    resolve: key => bindings.get(key),
    ...(options.installRenderArea === false
      ? {}
      : {
        renderArea: (binding, { empty, children }) => binding.key === undefined
          ? <>{empty?.() ?? null}</>
          : <Fragment key={binding.key}>{children}</Fragment>,
      }),
  }
  const rootEntry: StoredEntry = {
    component: (props: {
      renderSlot: (key: string, owner: object) => React.ReactNode
      SessionProvider: SessionProviderComponent
    }) => <>{bodies.root(props.renderSlot, props.SessionProvider)}</>,
    options: {},
    children: { 'k.session': { kind: 'single', scope: 'session' } },
  }
  const host: SlotRendererHost = {
    subscribe: () => () => {},
    getVersion: () => 0,
    entriesOf: key => key === 'root' ? [rootEntry] : sessionEntries,
    // Single-kind everywhere and no crashes in this suite: the projection is
    // the raw view and crash reports never fire.
    entriesOfSlot: key => key === 'root' ? [rootEntry] : sessionEntries,
    reportEntryError: () => {},
    specOf: key => key === 'k.session' ? { kind: 'single', scope: 'session' } : undefined,
    isLive: () => true,
    storeOf: () => undefined,
    root,
    scopeRevision: observable(0),
    scope: () => sessionAdapter,
  }
  return {
    host,
    // Driver surface: set(id) publishes the resolved binding (or the absent
    // projection) through the scope adapter.
    current: {
      set: (id: string | undefined) => {
        currentId = id
        currentBinding.set((id === undefined ? undefined : bindings.get(id)) ?? absentBinding)
      },
    },
    addSession: (id: string) => {
      // Bare source per binding (identity-stable): the machinery binds useSession from it.
      const binding: SessionBinding = {
        key: id,
        ctx: scopeCtx,
        hooks: { session: { getSnapshot: () => ({ sid: id }), subscribe: () => () => {} } },
        keyedHooks: {},
        props: { sessionId: id },
      }
      bindings.set(id, binding)
      if (currentId === id) currentBinding.set(binding)
      return binding
    },
    /** Swap one session's binding in place (roster-change stand-in); republish when current. */
    replaceSession: (binding: SessionBinding) => {
      bindings.set(binding.key, binding)
      if (currentId === binding.key) currentBinding.set(binding)
    },
    registerSession: (entry: StoredEntry) => { sessionEntries.push(entry) },
  }
}

describe('SessionProvider', () => {
  it('renders empty without a current session, switches to the body on select, falls back on an unresolvable id', () => {
    const h = makeHost({
      root: (_renderSlot, SessionProvider) => (
        <SessionProvider empty={() => <span>empty</span>}>
          <div data-testid="body">session</div>
        </SessionProvider>
      ),
    })
    h.addSession('s1')
    const view = render(<>{createSlotRenderer().renderRoot(h.host, {})}</>)
    expect(view.container.textContent).toBe('empty')
    act(() => { h.current.set('s1') })
    expect(view.container.textContent).toBe('session')
    act(() => { h.current.set('ghost') })   // listed nowhere: cell() misses
    expect(view.container.textContent).toBe('empty')
  })

  it('renders null empty state when the empty prop is omitted', () => {
    const h = makeHost({
      root: (_renderSlot, SessionProvider) => <SessionProvider><b>session</b></SessionProvider>,
    })
    const view = render(<>{createSlotRenderer().renderRoot(h.host, {})}</>)
    expect(view.container.textContent).toBe('')
  })

  it('remounts the body on session switch (key semantics) but not on unrelated re-renders', () => {
    let mounts = 0
    function Body({ id }: { id: string }) {
      const mounted = useRef(false)
      useEffect(() => {
        /* v8 ignore next -- strict-mode double-invoke guard, not a branch under test */
        if (!mounted.current) { mounted.current = true; mounts += 1 }
      }, [])
      return <div>{id}</div>
    }
    const h = makeHost({
      root: (renderSlot, SessionProvider) => (
        <SessionProvider>{renderSlot('k.session', {})}</SessionProvider>
      ),
    })
    h.registerSession({
      component: (props: { sessionId?: string }) => <Body id={props.sessionId ?? 'missing'} />,
      options: {},
    })
    h.addSession('s1')
    h.addSession('s2')
    const view = render(<>{createSlotRenderer().renderRoot(h.host, {})}</>)
    act(() => { h.current.set('s1') })
    const afterS1 = mounts
    act(() => { h.current.set('s2') })
    expect(mounts).toBe(afterS1 + 1)
    const afterS2 = mounts
    view.rerender(<>{createSlotRenderer().renderRoot(h.host, {})}</>)
    expect(mounts).toBe(afterS2)
  })

  it('delivers the resolved cell to session slots under it (observable behavior, not context internals)', () => {
    const seen: Record<string, unknown>[] = []
    const h = makeHost({
      root: (renderSlot, SessionProvider) => (
        <SessionProvider>{renderSlot('k.session', {})}</SessionProvider>
      ),
    })
    h.addSession('s1')
    h.addSession('s2')
    h.registerSession({
      component: (props: { useSession?: <S>(sel: (s: { sid: string }) => S) => S; sessionId?: string }) => {
        // The bound hook reads the cell's bare source — asserting through it
        // proves the machinery wired THIS session's source, not another's.
        seen.push({ sessionId: props.sessionId, read: props.useSession!(s => s.sid) })
        return null
      },
      options: {},
    })
    render(<>{createSlotRenderer().renderRoot(h.host, {})}</>)
    act(() => { h.current.set('s1') })
    expect(seen.at(-1)!['read']).toBe('s1')
    expect(seen.at(-1)!['sessionId']).toBe('s1')
    act(() => { h.current.set('s2') })
    expect(seen.at(-1)!['read']).toBe('s2')
    expect(seen.at(-1)!['sessionId']).toBe('s2')
  })

  it('republishes a mounted session entry when its provide bundle changes under the same id', () => {
    const seen: unknown[] = []
    const h = makeHost({
      root: (renderSlot, SessionProvider) => (
        <SessionProvider>{renderSlot('k.session', {})}</SessionProvider>
      ),
    })
    const original = h.addSession('s1')
    h.registerSession({
      component: (props: { feature?: string }) => {
        seen.push(props.feature)
        return null
      },
      options: {},
    })
    render(<>{createSlotRenderer().renderRoot(h.host, {})}</>)
    act(() => { h.current.set('s1') })
    expect(seen.at(-1)).toBeUndefined()
    // A provider-roster change rematerializes the binding; the scope source
    // must carry it to already-mounted entries without a selection change.
    act(() => { h.replaceSession({ ...original, props: { feature: 'now-live' } }) })
    expect(seen.at(-1)).toBe('now-live')
  })

  it('fails loud when the Session scope owner omits its area renderer', () => {
    const h = makeHost({ root: () => null }, { installRenderArea: false })
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    expect(() => render(<>{createSlotRenderer().renderRoot(h.host, {})}</>))
      .toThrow(/does not provide its area renderer/)
    spy.mockRestore()
  })
})
