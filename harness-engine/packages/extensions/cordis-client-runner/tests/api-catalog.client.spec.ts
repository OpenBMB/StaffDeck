import { describe, expect, it } from 'vitest'
import { EVENT_API, queryServiceApi, SERVICE_API } from '../src/client/api-catalog.ts'

describe('Client Cordis inspect catalog', () => {
  it('publishes the split Workspace Controller and UI navigation services', () => {
    expect(SERVICE_API.find(service => service.key === 'workspaces')?.methods.map(method => method.signature))
      .toEqual([
        'create(input: { path: string }): Promise<WorkspaceView>',
        'rename(workspaceId: WorkspaceId, title: string): Promise<WorkspaceView>',
        'delete(workspaceId: WorkspaceId): Promise<void>',
        'archiveSession(sessionId: SessionId): Promise<void>',
        'insertSessionBefore( workspaceId: WorkspaceId, sessionId: SessionId, beforeSessionId?: SessionId, ): Promise<WorkspaceView>',
      ])
    expect(SERVICE_API.find(service => service.key === 'uiWorkspace')?.methods.map(method => method.signature))
      .toEqual([
        'connectWorkspace(workspaceId: WorkspaceId): Promise<SessionId>',
        'startSession(workspaceId?: WorkspaceId): void',
        'archiveSession(sessionId: SessionId): Promise<void>',
        'pickDirectory(): Promise<string | null>',
        'listDirectory(path?: string, signal?: AbortSignal): Promise<DirectoryListing>',
        'createDirectory(path: string, name: string): Promise<string>',
      ])
  })

  it('contains one entry per visible Client event', () => {
    const names = EVENT_API.map(event => event.name)
    expect(new Set(names).size).toBe(names.length)
  })

  it('includes the current referenced type closure for the Sessions service', () => {
    const result = queryServiceApi('sessions') as {
      referencedTypes: readonly { name: string }[]
    }
    expect(result.referencedTypes.length).toBeGreaterThan(0)
    expect(result.referencedTypes.map(type => type.name)).not.toEqual(expect.arrayContaining([
      'ConversationSnapshot',
      'PendingInteraction',
      'PendingPayloads',
      'PendingWait',
    ]))
  })
})
