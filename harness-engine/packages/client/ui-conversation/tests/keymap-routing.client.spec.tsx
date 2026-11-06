// @vitest-environment jsdom
/**
 * Keymap routing at the DOM boundary: synthetic keydowns on the
 * contenteditable reach the registered composer commands (the jsdom lane's
 * gesture entry, below the full component bench).
 */
import { describe, expect, it, vi } from 'vitest'
import { fireEvent } from '@testing-library/react'
import { createEditor } from 'lexical'
import { registerPlainText } from '@lexical/plain-text'
import { registerComposerKeymap } from '../src/client/input/editor/keymap.ts'

describe('keymap keydown routing', () => {
  it('routes Enter to the keymap submit handler', () => {
    const editor = createEditor({ namespace: 'keymap-routing', onError: (e) => { throw e } })
    const root = document.createElement('div')
    root.contentEditable = 'true'
    document.body.appendChild(root)
    editor.setRootElement(root)
    registerPlainText(editor)
    const submit = vi.fn()
    registerComposerKeymap(editor, {
      arbitrate: () => 'pass',
      space: () => false,
      dismissPopup: () => {},
      canSubmit: () => true,
      submit,
      intakeFiles: () => {},
      pasteText: () => {},
    })
    fireEvent.keyDown(root, { key: 'Enter' })
    expect(submit).toHaveBeenCalledWith(false)
    fireEvent.keyDown(root, { key: 'Enter', metaKey: true })
    expect(submit).toHaveBeenCalledWith(true)
  })

  it('routes Tab through arbitration and passes when unconsumed', () => {
    const editor = createEditor({ namespace: 'keymap-routing', onError: (e) => { throw e } })
    const root = document.createElement('div')
    root.contentEditable = 'true'
    document.body.appendChild(root)
    editor.setRootElement(root)
    registerPlainText(editor)
    const arbitrate = vi.fn<(key: string, composing: boolean) => 'consumed' | 'pick-highlighted' | 'pass'>()
      .mockReturnValueOnce('consumed')
      .mockReturnValue('pass')
    registerComposerKeymap(editor, {
      arbitrate,
      space: () => false,
      dismissPopup: () => {},
      canSubmit: () => true,
      submit: () => {},
      intakeFiles: () => {},
      pasteText: () => {},
    })
    const consumed = fireEvent.keyDown(root, { key: 'Tab', keyCode: 9 })
    expect(arbitrate).toHaveBeenCalledWith('tab', false)
    expect(consumed).toBe(false) // consumed: preventDefault fired
    const passed = fireEvent.keyDown(root, { key: 'Tab', keyCode: 9 })
    expect(passed).toBe(true) // pass: the browser keeps native focus traversal
  })

})
