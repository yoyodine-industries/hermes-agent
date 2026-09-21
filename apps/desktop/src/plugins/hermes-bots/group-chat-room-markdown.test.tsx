// A room entry is a message a bot wrote, so the room's text surface has to be the
// SDK's canonical markdown renderer — the one that runs the transcript directive
// parser. Rendering entries with raw `Streamdown` is how a research render's
// `::preview{file="…"}` arrived in a room as literal prose instead of the report.
import { cleanup, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, expect, it, vi } from 'vitest'

import { translateBots } from './i18n-test-helper'

vi.mock('@hermes/plugin-sdk', async () => {
  const { pluginSdkMock, createGroupGateway } = await import('./group-test-utils')
  const base = await pluginSdkMock(createGroupGateway().host)

  const Button = ({ children, onClick, title }: { children?: ReactNode; onClick?: () => void; title?: string }) => (
    <button onClick={onClick} title={title}>
      {children}
    </button>
  )

  return {
    ...base,
    Button,
    RowButton: Button,
    cn: (...values: unknown[]) => values.filter(Boolean).join(' '),
    Codicon: () => null,
    CopyButton: () => null,
    ConfirmDialog: () => null,
    Dialog: () => null,
    DialogContent: () => null,
    DialogDescription: () => null,
    DialogFooter: () => null,
    DialogHeader: () => null,
    DialogTitle: () => null,
    Input: () => null,
    Tip: ({ children }: { children: ReactNode }) => children,
    relativeTime: () => 'now',
    useI18n: () => ({ t: { common: { cancel: 'Cancel', save: 'Save' } } }),
    usePluginI18n: () => translateBots,
    // Which of these a room entry lands in IS the assertion. They are stubs: the
    // real renderer's directive contract is proven in
    // `src/sdk/markdown-text-content.directives.test.tsx`.
    MarkdownTextContent: ({ text }: { text: string }) => <p data-testid="room-markdown" data-text={text} />,
    Streamdown: ({ children }: { children?: ReactNode }) => <p data-testid="room-raw-streamdown">{children}</p>
  }
})
vi.mock('./avatar', () => ({ avatarColor: () => '#888', botAppearance: () => ({}), BotFace: () => null }))
vi.mock('./group-chat-parts', () => ({
  GroupClarifyCard: () => null,
  GroupImageControls: () => null,
  GroupMentionInput: () => null
}))
afterEach(cleanup)

it("renders a room entry through the SDK's markdown renderer, never through raw Streamdown", async () => {
  Element.prototype.scrollIntoView = vi.fn()
  const { $groupChats } = await import('./group-chat')
  const { GroupChatWorkspace } = await import('./group-chat-view')

  const render_ = '::preview{file="/opt/hermes_sandbox/research/report.html"}'
  const prose = 'Findings are in the attached render.'

  $groupChats.set({
    Room: {
      log: [
        { id: 'm1', thread: 'a', from: { kind: 'member' as const, name: 'Researcher' }, text: render_, at: 1 },
        { id: 'm2', thread: 'a', from: { kind: 'member' as const, name: 'Builder' }, text: prose, at: 2 }
      ],
      sessions: {},
      watermarks: {}
    }
  })

  render(<GroupChatWorkspace group="Room" members={[]} />)

  const surfaces = screen.getAllByTestId('room-markdown').map(node => node.getAttribute('data-text'))

  expect(surfaces).toEqual([render_, prose])
  expect(screen.queryByTestId('room-raw-streamdown')).toBeNull()
})
