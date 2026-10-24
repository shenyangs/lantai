import { describe, expect, it } from "vitest";
import {
  createAsyncStateScope,
  createLatestRequestGate,
} from "./latestRequest";

describe("createLatestRequestGate", () => {
  it("lets only the newest overlapping request commit", () => {
    const gate = createLatestRequestGate();
    const firstOwnsState = gate.begin();
    const secondOwnsState = gate.begin();

    expect(firstOwnsState()).toBe(false);
    expect(secondOwnsState()).toBe(true);
  });

  it("invalidates an in-flight request when its owner unmounts or changes", () => {
    const gate = createLatestRequestGate();
    const ownsState = gate.begin();

    gate.invalidate();

    expect(ownsState()).toBe(false);
  });

  it("keeps concurrent writes valid until their owning scope changes", () => {
    const scope = createAsyncStateScope();
    const firstWriteOwnsState = scope.capture();
    const secondWriteOwnsState = scope.capture();

    expect(firstWriteOwnsState()).toBe(true);
    expect(secondWriteOwnsState()).toBe(true);

    scope.invalidate();

    expect(firstWriteOwnsState()).toBe(false);
    expect(secondWriteOwnsState()).toBe(false);
    expect(scope.capture()()).toBe(true);
  });
});
