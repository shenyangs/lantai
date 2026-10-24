import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  DEFAULT_SPIN_COUNT,
  MAX_SPIN_COUNT,
  hashProtectionPassword,
  resolveSpinCount,
  verifyProtectionPassword,
} from '../src/protection'

afterEach(() => {
  vi.restoreAllMocks()
})

describe('resolveSpinCount', () => {
  it('defaults missing values and accepts bounded non-negative integers', () => {
    expect(resolveSpinCount(undefined)).toBe(DEFAULT_SPIN_COUNT)
    expect(resolveSpinCount(null)).toBe(DEFAULT_SPIN_COUNT)
    expect(resolveSpinCount('')).toBe(DEFAULT_SPIN_COUNT)
    expect(resolveSpinCount(0)).toBe(0)
    expect(resolveSpinCount(1000)).toBe(1000)
    expect(resolveSpinCount('50000')).toBe(50000)
    expect(resolveSpinCount(MAX_SPIN_COUNT)).toBe(MAX_SPIN_COUNT)
    expect(DEFAULT_SPIN_COUNT).toBe(100000)
  })

  it('clamps hostile counts and rejects malformed values before hashing', () => {
    expect(MAX_SPIN_COUNT).toBe(1000000)
    expect(resolveSpinCount(99_999_999)).toBe(MAX_SPIN_COUNT)
    expect(() => resolveSpinCount(-1)).toThrow(/spinCount/)
    expect(() => resolveSpinCount(1.5)).toThrow(/spinCount/)
    expect(() => resolveSpinCount(NaN)).toThrow(/spinCount/)
    expect(() => resolveSpinCount(Infinity)).toThrow(/spinCount/)
    expect(() => resolveSpinCount('abc')).toThrow(/spinCount/)
  })
})

describe('verifyProtectionPassword DoS guard', () => {
  const salt = 'AAAAAAAAAAAAAAAAAAAAAA=='

  it('fails closed on absurd spinCount without hashing or throwing', async () => {
    const digest = vi.spyOn(globalThis.crypto.subtle, 'digest')
    await expect(
      verifyProtectionPassword('pw', {
        hash: 'eA==',
        salt,
        spinCount: 99_999_999,
        algorithmSid: 14,
      }),
    ).resolves.toBe(false)
    await expect(
      verifyProtectionPassword('pw', {
        hash: 'eA==',
        salt,
        spinCount: '99999999' as unknown as number,
        algorithmSid: 14,
      }),
    ).resolves.toBe(false)
    expect(digest).not.toHaveBeenCalled()
  })

  it('rejects invalid counts without calling Subtle.digest', async () => {
    const digest = vi.spyOn(globalThis.crypto.subtle, 'digest')
    await expect(
      verifyProtectionPassword('pw', {
        hash: 'eA==',
        salt,
        spinCount: -5,
        algorithmSid: 14,
      }),
    ).rejects.toThrow(/spinCount/)
    await expect(
      verifyProtectionPassword('pw', {
        hash: 'eA==',
        salt,
        spinCount: 1.5,
        algorithmSid: 14,
      }),
    ).rejects.toThrow(/spinCount/)
    expect(digest).not.toHaveBeenCalled()
  })

  it('returns false for a missing salt instead of hashing or throwing', async () => {
    const digest = vi.spyOn(globalThis.crypto.subtle, 'digest')
    await expect(
      verifyProtectionPassword('pw', {
        hash: 'eA==',
        spinCount: 1000,
        algorithmSid: 14,
      }),
    ).resolves.toBe(false)
    expect(digest).not.toHaveBeenCalled()
  })

  it('still verifies a password with a normal custom count', async () => {
    const credentials = await hashProtectionPassword('correct', 1000)
    await expect(verifyProtectionPassword('correct', credentials)).resolves.toBe(true)
    await expect(verifyProtectionPassword('wrong', credentials)).resolves.toBe(false)
  })
})
