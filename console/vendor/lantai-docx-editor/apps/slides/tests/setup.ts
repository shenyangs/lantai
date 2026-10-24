import { beforeEach } from 'vitest'

class MemoryStorage implements Storage {
  private values = new Map<string, string>()

  get length(): number {
    return this.values.size
  }

  clear(): void {
    this.values.clear()
  }

  getItem(key: string): string | null {
    return this.values.get(String(key)) ?? null
  }

  key(index: number): string | null {
    return Array.from(this.values.keys())[index] ?? null
  }

  removeItem(key: string): void {
    this.values.delete(String(key))
  }

  setItem(key: string, value: string): void {
    this.values.set(String(key), String(value))
  }
}

// Node 26 exposes an experimental global localStorage getter that returns
// undefined unless --localstorage-file is set. Replace it in jsdom tests with
// deterministic per-test storage; Electron's renderer storage is unaffected.
const storage = new MemoryStorage()
Object.defineProperty(globalThis, 'localStorage', {
  configurable: true,
  value: storage,
})
Object.defineProperty(window, 'localStorage', {
  configurable: true,
  value: storage,
})

beforeEach(() => storage.clear())
