import { chmod, mkdir } from 'node:fs/promises'
import { arch, platform } from 'node:os'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'

if (platform() !== 'darwin') process.exit(0)

const here = dirname(fileURLToPath(import.meta.url))
const appRoot = resolve(here, '..')
const targetArch = arch() === 'x64' ? 'x64' : 'arm64'
const output = resolve(appRoot, 'generated-native', `darwin-${targetArch}`, 'lantai-pdf-vision')
await mkdir(dirname(output), { recursive: true })

const result = spawnSync(
  'xcrun',
  [
    'swiftc',
    '-O',
    '-framework',
    'AppKit',
    '-framework',
    'PDFKit',
    '-framework',
    'Vision',
    resolve(appRoot, 'native', 'lantai-pdf-vision.swift'),
    '-o',
    output,
  ],
  { encoding: 'utf8' },
)

if (result.status !== 0) {
  process.stderr.write(result.stderr || result.stdout || 'Unable to compile lantai-pdf-vision\n')
  process.exit(result.status ?? 1)
}
await chmod(output, 0o755)
process.stdout.write(`${output}\n`)
