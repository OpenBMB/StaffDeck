/** Question-composer Session store behavior. */
import { describe, expect, it } from 'vitest'
import { createQuestionDraftStore, type QuestionDraftProgress } from '../src/client/draft-store.ts'

const FIRST: QuestionDraftProgress = {
  index: 1,
  drafts: [{ selected: ['Fast'], custom: '', skipped: false }],
}

describe('createQuestionDraftStore', () => {
  it('keeps one request progress and ignores cleanup from an obsolete request', () => {
    const store = createQuestionDraftStore().create('session-one')

    store.actions.replace('question:one', FIRST)
    expect(store.getSnapshot()).toEqual({ requestKey: 'question:one', progress: FIRST })

    store.actions.clear('question:older')
    expect(store.getSnapshot()).toEqual({ requestKey: 'question:one', progress: FIRST })

    store.actions.clear('question:one')
    expect(store.getSnapshot()).toEqual({ progress: { index: 0, drafts: [] } })
  })

  it('replaces the previous request atomically instead of accumulating drafts', () => {
    const store = createQuestionDraftStore().create('session-one')
    const second: QuestionDraftProgress = {
      index: 0,
      drafts: [{ selected: [], custom: 'Careful', skipped: false }],
    }

    store.actions.replace('question:one', FIRST)
    store.actions.replace('question:two', second)

    expect(store.getSnapshot()).toEqual({ requestKey: 'question:two', progress: second })
  })
})
