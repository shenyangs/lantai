import type { DocProtection } from './types'

/**
 * w:documentProtection password hash (Word 2013+ scheme, ECMA-376 iterated hash):
 *   h0 = H(salt || UTF-16LE(password))
 *   hi = H(h(i-1) || LE32(i))   i = 0..spinCount-1
 * sid 14 = SHA-512 (the only implemented algorithm). Uses WebCrypto, so it works in
 * both the renderer process and Node 20+.
 */

/** Word's default iteration count for newly generated password hashes. */
export const DEFAULT_SPIN_COUNT = 100000

/**
 * Upper bound for untrusted iteration counts read from document XML.
 * Every iteration invokes SubtleCrypto.digest, so leaving this unbounded
 * lets a hostile document stall the editor during password verification.
 */
export const MAX_SPIN_COUNT = 1000000

/** Validate and normalize a spin count before any hashing starts. */
export function resolveSpinCount(raw: unknown): number {
  if (raw === undefined || raw === null || raw === '') return DEFAULT_SPIN_COUNT
  const value = typeof raw === 'string' ? Number(raw.trim()) : raw
  if (typeof value !== 'number' || !Number.isFinite(value) || !Number.isInteger(value)) {
    throw new Error(`invalid spinCount: ${String(raw)}`)
  }
  if (value < 0) throw new Error(`invalid spinCount: ${String(raw)}`)
  return Math.min(value, MAX_SPIN_COUNT)
}

function toBase64(bytes: Uint8Array): string {
  let bin = ''
  for (const b of bytes) bin += String.fromCharCode(b)
  return btoa(bin)
}

function fromBase64(b64: string): Uint8Array {
  const bin = atob(b64)
  const out = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i)
  return out
}

function utf16le(text: string): Uint8Array {
  const out = new Uint8Array(text.length * 2)
  for (let i = 0; i < text.length; i++) {
    const code = text.charCodeAt(i)
    out[i * 2] = code & 0xff
    out[i * 2 + 1] = code >> 8
  }
  return out
}

function concat(a: Uint8Array, b: Uint8Array): Uint8Array {
  const out = new Uint8Array(a.length + b.length)
  out.set(a, 0)
  out.set(b, a.length)
  return out
}

async function iteratedSha512(
  password: string,
  salt: Uint8Array,
  spinCount: number,
): Promise<Uint8Array> {
  const subtle = globalThis.crypto.subtle
  const digest = async (bytes: Uint8Array) =>
    new Uint8Array(await subtle.digest('SHA-512', bytes as unknown as ArrayBuffer))
  let hash = await digest(concat(salt, utf16le(password)))
  const iter = new Uint8Array(4)
  const view = new DataView(iter.buffer)
  for (let i = 0; i < spinCount; i++) {
    view.setUint32(0, i, true)
    hash = await digest(concat(hash, iter))
  }
  return hash
}

/** Generate the protection password hash (random 16-byte salt, 100000 iterations by default) */
export async function hashProtectionPassword(
  password: string,
  spinCount = DEFAULT_SPIN_COUNT,
): Promise<{ hash: string; salt: string; spinCount: number; algorithmSid: number }> {
  const resolvedSpinCount = resolveSpinCount(spinCount)
  const salt = globalThis.crypto.getRandomValues(new Uint8Array(16))
  const hash = await iteratedSha512(password, salt, resolvedSpinCount)
  return {
    hash: toBase64(hash),
    salt: toBase64(salt),
    spinCount: resolvedSpinCount,
    algorithmSid: 14,
  }
}

/** Check whether the password matches the hash in documentProtection (no hash = no password, always true) */
export async function verifyProtectionPassword(
  password: string,
  protection: Pick<DocProtection, 'hash' | 'salt' | 'spinCount' | 'algorithmSid'>,
): Promise<boolean> {
  if (!protection.hash) return true
  if ((protection.algorithmSid ?? 14) !== 14) return false // only SHA-512 is supported
  if (!protection.salt) return false

  const rawSpinCount: unknown = protection.spinCount ?? DEFAULT_SPIN_COUNT
  const numericSpinCount =
    typeof rawSpinCount === 'string' ? Number(rawSpinCount.trim()) : rawSpinCount
  if (
    typeof numericSpinCount === 'number' &&
    Number.isFinite(numericSpinCount) &&
    numericSpinCount > MAX_SPIN_COUNT
  ) {
    // Fail closed without burning up to a million digests for a value that
    // cannot match the document's declared hash after normalization.
    return false
  }
  const spinCount = resolveSpinCount(numericSpinCount)
  const salt = fromBase64(protection.salt)
  const hash = await iteratedSha512(password, salt, spinCount)
  return toBase64(hash) === protection.hash
}
