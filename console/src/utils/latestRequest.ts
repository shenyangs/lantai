export interface LatestRequestGate {
  begin: () => () => boolean;
  invalidate: () => void;
}

export interface AsyncStateScope {
  capture: () => () => boolean;
  invalidate: () => void;
}

/**
 * Gives every async read a monotonic ownership token.
 *
 * A response may update UI state only while the predicate returned by
 * `begin()` is true. Starting a newer read or invalidating the gate makes all
 * older predicates false, preventing data from a previous agent, route, or
 * selection from overwriting the current surface.
 */
export function createLatestRequestGate(): LatestRequestGate {
  let generation = 0;
  return {
    begin() {
      const ownedGeneration = ++generation;
      return () => generation === ownedGeneration;
    },
    invalidate() {
      generation += 1;
    },
  };
}

/**
 * Captures ownership of a route/agent/selection scope without making
 * concurrent operations inside that scope invalidate each other.
 *
 * Use this for writes: several mutations may legitimately run together, but
 * none of their completions may update the UI after the owning scope changes.
 */
export function createAsyncStateScope(): AsyncStateScope {
  let generation = 0;
  return {
    capture() {
      const ownedGeneration = generation;
      return () => generation === ownedGeneration;
    },
    invalidate() {
      generation += 1;
    },
  };
}
