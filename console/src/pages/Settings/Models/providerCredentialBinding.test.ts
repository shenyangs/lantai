import { describe, expect, it } from "vitest";
import {
  getCredentialOwnershipAttestation,
  requiresCredentialOwnershipAttestation,
} from "./providerCredentialBinding";

describe("TokenDance manual credential ownership", () => {
  it("requires and forwards explicit attestation only for a new TokenDance key", () => {
    expect(
      requiresCredentialOwnershipAttestation("tokendance", "new-key"),
    ).toBe(true);
    expect(
      getCredentialOwnershipAttestation("tokendance", "new-key", false),
    ).toBe(false);
    expect(
      getCredentialOwnershipAttestation("tokendance", "new-key", true),
    ).toBe(true);
    expect(
      getCredentialOwnershipAttestation("tokendance", "", false),
    ).toBeUndefined();
    expect(
      getCredentialOwnershipAttestation("openai", "new-key", true),
    ).toBeUndefined();
  });
});
