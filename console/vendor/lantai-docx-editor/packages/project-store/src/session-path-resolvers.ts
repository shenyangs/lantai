export type ProjectSessionPathResolver = (
  senderId: number,
  sessionId: string,
) => string | null

const resolverRegistryKey = Symbol.for('lantai.project-session-path-resolvers')

type GlobalResolverRegistry = typeof globalThis & {
  [resolverRegistryKey]?: Set<ProjectSessionPathResolver>
}

function getResolverRegistry(): Set<ProjectSessionPathResolver> {
  const scope = globalThis as GlobalResolverRegistry
  if (!scope[resolverRegistryKey]) {
    scope[resolverRegistryKey] = new Set<ProjectSessionPathResolver>()
  }
  return scope[resolverRegistryKey]
}

/**
 * Register an editor-specific session lookup in the Electron main process.
 * Independently bundled Office runtimes share this Symbol.for-backed registry.
 */
export function registerProjectSessionPathResolver(
  resolver: ProjectSessionPathResolver,
): () => void {
  const registry = getResolverRegistry()
  registry.add(resolver)
  return () => registry.delete(resolver)
}

export function resolveProjectSessionPath(
  senderId: number,
  sessionId: string,
): string | null {
  for (const resolver of getResolverRegistry()) {
    const path = resolver(senderId, sessionId)
    if (path) return path
  }
  return null
}
