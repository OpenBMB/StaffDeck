import { useCallback, useContext, useEffect, useRef } from 'react';
import { UNSAFE_NavigationContext } from 'react-router-dom';

export const UNSAVED_MESSAGE = '有未保存修改，确定放弃并离开吗？';

/** BrowserRouter does not support useBlocker; guard its navigator and browser exits. */
export function useWorkbenchDirtyGuard(dirty: boolean) {
  const context = useContext(UNSAFE_NavigationContext);
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;
  const confirmLeave = useCallback(() => !dirtyRef.current || window.confirm(UNSAVED_MESSAGE), []);
  useEffect(() => {
    const navigator = context?.navigator;
    if (!navigator) return;
    const push = navigator.push;
    const replace = navigator.replace;
    const go = navigator.go;
    let lastIndex = window.history.state?.idx as number | undefined;
    let restoring = false;
    let approvedGo = false;
    navigator.push = (...args) => { if (confirmLeave()) { push.apply(navigator, args); lastIndex = window.history.state?.idx; } };
    navigator.replace = (...args) => { if (confirmLeave()) { replace.apply(navigator, args); lastIndex = window.history.state?.idx; } };
    navigator.go = (...args) => { if (confirmLeave()) { approvedGo = true; go.apply(navigator, args); } };
    const popState = (event: PopStateEvent) => {
      const nextIndex = event.state?.idx as number | undefined;
      if (restoring) { restoring = false; event.stopImmediatePropagation(); return; }
      if (!approvedGo && dirtyRef.current && !confirmLeave()) {
        if (typeof lastIndex === 'number' && typeof nextIndex === 'number') {
          event.stopImmediatePropagation(); restoring = true; window.history.go(lastIndex - nextIndex);
        }
      } else { lastIndex = nextIndex; }
      approvedGo = false;
    };
    const beforeUnload = (event: BeforeUnloadEvent) => {
      if (dirtyRef.current) { event.preventDefault(); event.returnValue = ''; }
    };
    window.addEventListener('beforeunload', beforeUnload);
    window.addEventListener('popstate', popState, true);
    return () => {
      navigator.push = push; navigator.replace = replace; navigator.go = go;
      window.removeEventListener('beforeunload', beforeUnload);
      window.removeEventListener('popstate', popState, true);
    };
  }, [context?.navigator, confirmLeave]);
  return confirmLeave;
}
