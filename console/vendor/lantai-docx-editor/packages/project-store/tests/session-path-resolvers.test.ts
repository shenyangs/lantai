import { afterEach, describe, expect, it } from 'vitest'

import {
  registerProjectSessionPathResolver,
  resolveProjectSessionPath,
} from '../src/session-path-resolvers.js'

const cleanups: Array<() => void> = []

afterEach(() => {
  while (cleanups.length > 0) cleanups.pop()?.()
})

describe('project session path resolvers', () => {
  it('lets independently bundled editors resolve another editor session', () => {
    cleanups.push(registerProjectSessionPathResolver(() => null))
    cleanups.push(
      registerProjectSessionPathResolver((senderId, sessionId) =>
        senderId === 42 && sessionId === 'sheet-1' ? '/tmp/report.xlsx' : null,
      ),
    )

    expect(resolveProjectSessionPath(42, 'sheet-1')).toBe('/tmp/report.xlsx')
    expect(resolveProjectSessionPath(7, 'sheet-1')).toBeNull()
  })

  it('removes one resolver without disturbing the others', () => {
    const removeFirst = registerProjectSessionPathResolver(() => '/tmp/first.xlsx')
    cleanups.push(removeFirst)
    cleanups.push(registerProjectSessionPathResolver(() => '/tmp/second.xlsx'))

    expect(resolveProjectSessionPath(1, 'session')).toBe('/tmp/first.xlsx')
    removeFirst()
    cleanups.splice(cleanups.indexOf(removeFirst), 1)
    expect(resolveProjectSessionPath(1, 'session')).toBe('/tmp/second.xlsx')
  })
})
