function copyWithSelection(text: string): boolean {
  if (typeof document === 'undefined' || !document.body || typeof document.execCommand !== 'function') {
    return false;
  }

  const activeElement = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  const selection = document.getSelection();
  const ranges = selection
    ? Array.from({ length: selection.rangeCount }, (_, index) => selection.getRangeAt(index).cloneRange())
    : [];
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.setAttribute('readonly', '');
  textarea.style.position = 'fixed';
  textarea.style.left = '0';
  textarea.style.top = '0';
  textarea.style.width = '1px';
  textarea.style.height = '1px';
  textarea.style.padding = '0';
  textarea.style.border = '0';
  textarea.style.opacity = '0.01';
  textarea.style.pointerEvents = 'none';
  document.body.appendChild(textarea);

  // Some restricted HTTP browsers return true from execCommand without
  // emitting a real copy operation. Supplying the payload through the copy
  // event improves compatibility and prevents a false-success result.
  let copyEventSeen = false;
  const handleCopy = (event: ClipboardEvent) => {
    copyEventSeen = true;
    if (!event.clipboardData) return;
    event.clipboardData.setData('text/plain', text);
    event.preventDefault();
  };
  document.addEventListener('copy', handleCopy, true);

  try {
    textarea.focus();
    textarea.select();
    textarea.setSelectionRange(0, textarea.value.length);
    return document.execCommand('copy') && copyEventSeen;
  } finally {
    document.removeEventListener('copy', handleCopy, true);
    textarea.remove();
    if (selection) {
      selection.removeAllRanges();
      ranges.forEach((range) => selection.addRange(range));
    }
    activeElement?.focus();
  }
}

/** Copies text in browsers, desktop webviews, and non-secure local deployments. */
export async function copyTextToClipboard(text: string): Promise<void> {
  const clipboard = typeof navigator === 'undefined' ? undefined : navigator.clipboard;
  if (clipboard?.writeText) {
    try {
      await clipboard.writeText(text);
      return;
    } catch {
      // Some desktop webviews expose Clipboard API but reject it due to permissions.
    }
  }

  if (!copyWithSelection(text)) {
    throw new Error('Clipboard access is unavailable');
  }
}
