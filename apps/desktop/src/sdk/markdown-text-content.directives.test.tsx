// Contract of the markdown surface the plugin SDK hands to plugins: it runs the
// transcript pipeline, so a `::name{...}` directive a plugin claimed renders as
// that plugin's component. Raw `Streamdown` — also exported by the SDK, and what
// the group-chat room view used to render every room entry with — has no such
// step and leaves the directive in the prose.

// Imported through the SDK on purpose: this is the exact module a plugin gets.
import { MarkdownTextContent, Streamdown } from '@hermes/plugin-sdk'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { registry } from '@/contrib/registry'
import { TRANSCRIPT_DIRECTIVE_AREA, type TranscriptDirectiveContribution } from '@/lib/transcript-directives'

const DIRECTIVE = '::demo{label="hi"}'

function claimDemoDirective() {
  return registry.register({
    id: 'test:sdk-markdown-demo',
    area: TRANSCRIPT_DIRECTIVE_AREA,
    source: 'plugin:test',
    data: {
      name: 'demo',
      render: ({ attrs }) => <div data-testid="demo-card">{attrs.label ?? 'demo'}</div>
    } satisfies TranscriptDirectiveContribution
  })
}

afterEach(cleanup)

describe('SDK markdown exports', () => {
  it('MarkdownTextContent renders a claimed directive as its card, leaving no raw directive text', () => {
    const dispose = claimDemoDirective()

    try {
      const { container } = render(<MarkdownTextContent isRunning={false} text={DIRECTIVE} />)

      expect(screen.getByTestId('demo-card').textContent).toBe('hi')
      expect(container.textContent ?? '').not.toContain('::demo')
    } finally {
      dispose()
    }
  })

  it('raw Streamdown leaks the same directive into the prose, which is why rooms must not use it', () => {
    const dispose = claimDemoDirective()

    try {
      const { container } = render(<Streamdown>{DIRECTIVE}</Streamdown>)

      expect(container.textContent ?? '').toContain(DIRECTIVE)
    } finally {
      dispose()
    }
  })
})
