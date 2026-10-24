export function requiresCredentialOwnershipAttestation(
  providerId: string,
  apiKey?: string,
): boolean {
  return providerId === "tokendance" && Boolean(apiKey?.trim());
}

export function getCredentialOwnershipAttestation(
  providerId: string,
  apiKey: string | undefined,
  attested: boolean | undefined,
): boolean | undefined {
  if (!requiresCredentialOwnershipAttestation(providerId, apiKey)) {
    return undefined;
  }
  return attested === true;
}
