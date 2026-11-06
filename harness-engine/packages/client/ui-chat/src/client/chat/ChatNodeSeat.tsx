import { memo, useCallback, useMemo } from 'react'
import { JsonBlock } from '@deepseek-ai/dsh-client-ui-primitives'
import type { ChatNodeOwnerProps, ChatViewSlotProps } from '../contract/slots.ts'
import type { ChatNode } from '../contract/chat-nodes.ts'
import type { ChatNodeStore } from '../contract/snapshot.ts'
import {
  decodeTurnProcess, TURN_PROCESS_INDEPENDENT_KINDS, turnProcessGeneration,
  type TurnProcessSpec,
} from '../contract/turn-process.ts'
import { storedTurnProcessEntry } from '../stores.ts'
import { useSearchableHidden } from './searchable-hidden.ts'
import css from './ChatView.module.css'

interface ChatNodeSeatProps extends ChatNodeOwnerProps {
  readonly nodeKey: string
  readonly historyIncomplete: boolean
  readonly compactTranscript: boolean
  readonly useChat: ChatViewSlotProps['useChat']
  readonly useStore: ChatViewSlotProps['useStore']
  readonly actions: ChatViewSlotProps['actions']
  readonly renderSlot: ChatViewSlotProps['renderSlot']
  readonly t: ChatViewSlotProps['t']
}

type RoutedChatNodeOwner = {
  [Kind in ChatNode['kind']]: ChatNodeOwnerProps & { readonly node: ChatNode<Kind> }
}[ChatNode['kind']]

const EMPTY_PROCESS_KEYS: readonly string[] = []

interface TurnProcessLayout {
  readonly hasExternalProcess: boolean
  readonly compactAnswer: boolean
}

function turnProcessOpeningHumanAnchor(
  keys: readonly string[],
  nodes: ChatNodeStore,
  spec: TurnProcessSpec,
): number | undefined {
  let anchor: number | undefined
  for (const key of keys) {
    const node = nodes.get(key) as ChatNode | undefined
    if ((node?.kind === 'user' || node?.kind === 'steering')
      && node.anchorSeq < spec.controlAnchorSeq) {
      anchor = Math.min(anchor ?? node.anchorSeq, node.anchorSeq)
    }
  }
  return anchor
}

/** Derive disclosure facts from one content-revisioned Turn index. */
function turnProcessLayout(
  keys: readonly string[],
  nodes: ChatNodeStore,
  spec: TurnProcessSpec,
): TurnProcessLayout {
  let hasExternalProcess = false
  let compactAnswer = true
  const openingHumanAnchor = turnProcessOpeningHumanAnchor(keys, nodes, spec)
  for (const key of keys) {
    const node = nodes.get(key) as ChatNode | undefined
    if (node === undefined || node.kind === 'turn-process') continue
    if ((node.kind === 'user' || node.kind === 'steering')
      && (openingHumanAnchor === undefined || node.anchorSeq > openingHumanAnchor)
      && (spec.answerAnchorSeq === null || node.anchorSeq < spec.answerAnchorSeq)) {
      compactAnswer = false
    }
    if (TURN_PROCESS_INDEPENDENT_KINDS.has(node.kind)
      || node.anchorSeq < spec.processStartSeq
      || (spec.answerAnchorSeq !== null && node.anchorSeq >= spec.answerAnchorSeq)) continue
    if (node.kind !== 'assistant-step' || spec.answerStep === null || node.data.step !== spec.answerStep) {
      hasExternalProcess = true
    }
  }
  return { hasExternalProcess, compactAnswer }
}

/** Subscribe, apply Turn-process visibility, and dispatch one stable Context key. */
export const ChatNodeSeat = memo(function ChatNodeSeat({
  nodeKey, historyIncomplete, compactTranscript,
  selectedCallId, cwd, openFile, inspectCall, forkAt,
  renderMessageImages, fileMentions, useChat, useStore, actions, renderSlot, t,
}: ChatNodeSeatProps) {
  const node = useChat(snapshot => snapshot.nodes.get(nodeKey))
  const processSignature = useChat((snapshot) => {
    const current = snapshot.nodes.get(nodeKey)
    const location = current?.location
    return location?.kind === 'turn' || location?.kind === 'step'
      ? location.turn.data.get('turn-process')
      : undefined
  })
  const processSpec = useMemo(
    () => processSignature === undefined ? undefined : decodeTurnProcess(processSignature),
    [processSignature],
  )
  const nodeStore = useChat(snapshot => snapshot.nodes)
  const processLayoutKeys = useChat((snapshot) => {
    if (!compactTranscript || historyIncomplete || processSpec === undefined) return EMPTY_PROCESS_KEYS
    const current = snapshot.nodes.get(nodeKey) as ChatNode | undefined
    const location = current?.location
    if (current === undefined
      || (location?.kind !== 'turn' && location?.kind !== 'step')
      || location.turn.status !== 'closed'
      || location.turn.turn !== processSpec.turn) return EMPTY_PROCESS_KEYS
    const ownsLayout = current.kind === 'turn-process'
      || (current.kind === 'assistant-step' && current.data.step === processSpec.answerStep)
    return ownsLayout ? snapshot.locations.getTurn(processSpec.turn) : EMPTY_PROCESS_KEYS
  })
  const processLayout = useMemo(
    () => processSpec === undefined || processLayoutKeys.length === 0
      ? undefined
      : turnProcessLayout(processLayoutKeys, nodeStore, processSpec),
    [nodeStore, processLayoutKeys, processSpec],
  )
  const processGeneration = useMemo(
    () => processSpec === undefined ? undefined : turnProcessGeneration(processSpec),
    [processSpec],
  )
  const storedEntry = useStore(state => processSpec === undefined
    ? undefined
    : storedTurnProcessEntry(state, processSpec.turn))
  const processEntry = storedEntry?.generation === processGeneration ? storedEntry : undefined
  const processOpen = processEntry !== undefined
  const setOpen = useCallback((open: boolean) => {
    if (processGeneration !== undefined && processSpec !== undefined) {
      actions.setTurnProcessOpen(processSpec.turn, processGeneration, open)
    }
  }, [actions, processGeneration, processSpec])
  const routedNode = node as ChatNode | undefined
  const sameTurn = routedNode !== undefined
    && processSpec !== undefined
    && (routedNode.location.kind === 'turn' || routedNode.location.kind === 'step')
    && routedNode.location.turn.turn === processSpec.turn
  const turnClosed = sameTurn
    && routedNode.location.turn.status === 'closed'
  const processWindowReady = processSpec !== undefined
    && compactTranscript
    && processSpec.answerAnchorSeq !== null
    && turnClosed
    && !historyIncomplete
  const processMember = sameTurn
    && processWindowReady
    && !TURN_PROCESS_INDEPENDENT_KINDS.has(routedNode.kind)
    && routedNode.anchorSeq >= processSpec.processStartSeq
    && routedNode.anchorSeq < processSpec.answerAnchorSeq
  const processAnswer = sameTurn
    && processWindowReady
    && routedNode.kind === 'assistant-step'
    && routedNode.data.step === processSpec.answerStep
  const ownsDisclosure = routedNode?.kind === 'turn-process' || processAnswer
  const foldable = processWindowReady
    && (processMember || (ownsDisclosure
      && ((processLayout?.hasExternalProcess ?? false) || processSpec.inlineReasoning)))
  const turnProcess = useMemo(() => processGeneration === undefined || processSpec === undefined
    ? undefined
    : {
      spec: processSpec,
      foldable,
      open: processOpen,
      setOpen,
    }, [
    foldable, processGeneration, processOpen, processSpec, setOpen,
  ])
  const controllerInactive = routedNode?.kind === 'turn-process'
    && !foldable
  const compactAnswer = processAnswer
    && foldable
    && processLayout?.compactAnswer === true
    && !processOpen
  const processHidden = controllerInactive || (foldable && processMember && !processOpen)
  const revealProcess = useCallback(() => {
    if (processMember) setOpen(true)
  }, [processMember, setOpen])
  const wrapperRef = useSearchableHidden(processHidden, revealProcess)
  const owner = useMemo<ChatNodeOwnerProps | null>(() => node === undefined
    ? null
    : {
      selectedCallId,
      cwd,
      openFile,
      inspectCall,
      forkAt,
      renderMessageImages,
      fileMentions,
      turnProcess,
    }, [
    node, selectedCallId, cwd, openFile, inspectCall, forkAt,
    renderMessageImages, fileMentions, turnProcess,
  ])
  if (routedNode === undefined || owner === null) return null
  const location = routedNode.location
  const turn = location.kind === 'turn' || location.kind === 'step'
    ? location.turn.turn
    : undefined
  // Runtime dispatch owns the correlation: every Node's discriminant is the
  // keyed-slot entry passed alongside that same Node. TypeScript does not
  // distribute an object containing a union into a union of objects itself.
  const routedOwner = { ...owner, node: routedNode } as RoutedChatNodeOwner
  return (
    <div
      ref={wrapperRef}
      className={css.flowItem}
      data-chat-anchor-key={routedNode.key}
      data-chat-flow-key={routedNode.key}
      data-chat-flow-kind={routedNode.kind}
      data-chat-turn={turn}
      data-turn-process-member={processMember || undefined}
      data-turn-process-hidden={processHidden || undefined}
      data-turn-process-answer={compactAnswer || undefined}
    >
      {renderSlot('conversation.chat.node', routedOwner, {
        entryKey: routedNode.kind,
        hookContext: nodeKey,
        fallback: (
          <JsonBlock
            label={t('message.unknownSurface', { type: routedNode.kind })}
            payload={routedNode.data}
            truncatedLabel={total => t('json.truncated', { total })}
          />
        ),
      })}
    </div>
  )
})
