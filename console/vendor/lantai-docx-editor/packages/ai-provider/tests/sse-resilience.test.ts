import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AgentToolCall, AgentToolDef } from '@genoffice/agent-core'
import { sseLines, streamForProvider } from '../src/stream'
import { okResponse, sseStream } from './test-utils'

afterEach(() => {
  vi.unstubAllGlobals()
})

function collector() {
  const deltas: string[] = []
  return {
    deltas,
    cb: {
      signal: new AbortController().signal,
      onDelta: (text: string) => deltas.push(text),
      onToolCall: (_call: AgentToolCall) => undefined,
    },
  }
}

function hangingBody(): ReadableStream<Uint8Array> {
  return new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new TextEncoder().encode('data: first\n\n'))
    },
  })
}

describe('sseLines cleanup', () => {
  it('releases the reader when the consumer abandons mid-stream', async () => {
    const body = hangingBody()
    const generator = sseLines(body)
    expect((await generator.next()).value).toBe('data: first')
    await generator.return(undefined)
    expect(() => body.getReader()).not.toThrow()
  })

  it('releases the reader when a consumer exception closes the generator', async () => {
    const body = hangingBody()
    const generator = sseLines(body)
    await generator.next()
    await expect(generator.throw(new Error('gateway error'))).rejects.toThrow('gateway error')
    expect(() => body.getReader()).not.toThrow()
  })
})

describe('provider streams tolerate a malformed SSE frame', () => {
  it.each([
    {
      provider: 'anthropic' as const,
      model: 'claude-test',
      frames: [
        'data: {not-json',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"kept"}}',
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}',
      ],
    },
    {
      provider: 'gemini' as const,
      model: 'gemini-test',
      frames: [
        'data: {not-json',
        'data: {"candidates":[{"content":{"parts":[{"text":"kept"}]},"finishReason":"STOP"}]}',
      ],
    },
    {
      provider: 'openai' as const,
      model: 'openai-test',
      frames: [
        'data: {not-json',
        'data: {"choices":[{"delta":{"content":"kept"},"finish_reason":"stop"}]}',
        'data: [DONE]',
      ],
    },
  ])(
    '$provider skips the corrupt frame and keeps valid output',
    async ({ provider, model, frames }) => {
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue(okResponse(sseStream(frames))))
      const { deltas, cb } = collector()
      await streamForProvider(provider, { apiKey: 'k', model }, 'system', [], [], 100, cb)
      expect(deltas.join('')).toBe('kept')
    },
  )
})

describe('Gemini tool schema compatibility', () => {
  it('normalizes nullable unions, local refs, and tuple items before sending tools', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        okResponse(
          sseStream([
            'data: {"candidates":[{"content":{"parts":[{"text":"ok"}]},"finishReason":"STOP"}]}',
          ]),
        ),
      )
    vi.stubGlobal('fetch', fetchMock)
    const tool: AgentToolDef = {
      name: 'edit_document',
      description: 'Edit through natural language',
      inputSchema: {
        type: 'object',
        definitions: {
          point: {
            type: 'object',
            properties: { value: { type: ['number', 'null'] } },
          },
        },
        properties: {
          point: { $ref: '#/definitions/point', description: 'Chart point' },
          tuple: {
            type: 'array',
            items: [{ type: 'string' }, { type: 'number' }],
          },
        },
        required: ['point'],
      },
    }
    await streamForProvider(
      'gemini',
      { apiKey: 'k', model: 'gemini-test' },
      'system',
      [],
      [tool],
      100,
      collector().cb,
    )

    const request = JSON.parse(fetchMock.mock.calls[0]![1].body as string) as {
      tools: Array<{
        functionDeclarations: Array<{ parameters: Record<string, unknown> }>
      }>
    }
    expect(request.tools[0]!.functionDeclarations[0]!.parameters).toEqual({
      type: 'object',
      properties: {
        point: {
          type: 'object',
          description: 'Chart point',
          properties: { value: { type: 'number', nullable: true } },
        },
        tuple: { type: 'array', items: { type: 'string' } },
      },
      required: ['point'],
    })
  })
})
