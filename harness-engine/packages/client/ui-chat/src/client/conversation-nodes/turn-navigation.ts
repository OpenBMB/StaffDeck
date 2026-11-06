import type { ChatNode } from '../contract/chat-nodes.ts'
import type { ChatLocationNodeIndex, ChatNodeStore, TurnNavigationItem } from '../contract/snapshot.ts'

/**
 * Preview budget per field. The rail clamps two short lines, so anything past
 * this is invisible; copying whole transcripts into navigation state would
 * otherwise grow with the loaded window on every structural update.
 */
const PREVIEW_LIMIT = 160

/** Join rendered text until the preview budget is met, then stop reading. */
function preview(parts: Iterable<string>): string {
  let text = ''
  for (const part of parts) {
    text += text === '' ? part : ` ${part}`
    if (text.length >= PREVIEW_LIMIT) break
  }
  return text.replace(/\s+/g, ' ').trim().slice(0, PREVIEW_LIMIT)
}

function promptText(node: ChatNode): string {
  if (node.kind !== 'user') return ''
  return preview(node.data.content.flatMap(block => block.type === 'text' ? [block.text] : []))
}

function responseText(node: ChatNode): string {
  if (node.kind !== 'assistant-step') return ''
  return preview(node.data.blocks.flatMap(block => block.kind === 'text' ? [block.text] : []))
}

/**
 * Whether two items carry the same rail state, so the reader can keep its array.
 * @param left - previously published item, when the Turn had one.
 * @param right - freshly derived item, when the Turn still has one.
 * @returns whether both sides describe the same mark.
 */
export function sameTurnNavigationItem(
  left: TurnNavigationItem | undefined,
  right: TurnNavigationItem | undefined,
): boolean {
  if (left === undefined || right === undefined) return left === right
  return left.turn === right.turn && left.anchorKey === right.anchorKey
    && left.prompt === right.prompt && left.response === right.response
}

/**
 * Project one loaded Turn into its rail item.
 * @param turn - Turn number the item addresses.
 * @param locations - live Location index supplying the Turn's node keys.
 * @param nodes - live Chat node store.
 * @returns the item, or undefined when the Turn has no visible loaded node.
 */
export function turnNavigationItem(
  turn: number,
  locations: ChatLocationNodeIndex,
  nodes: ChatNodeStore,
): TurnNavigationItem | undefined {
  const loaded = locations.getTurn(turn)
    .map(key => nodes.get(key))
    .filter((node): node is ChatNode => node !== undefined && node.visibility === 'visible')
  const user = loaded.find(node => node.kind === 'user')
  const anchor = user ?? loaded[0]
  if (anchor === undefined) return undefined
  const response = loaded.findLast(node => responseText(node) !== '')
  return {
    turn,
    anchorKey: anchor.key,
    prompt: user === undefined ? '' : promptText(user),
    response: response === undefined ? '' : responseText(response),
  }
}
