export interface OfficeWorkbenchIpcFailure {
  success: false;
  error?: string;
  error_code?: string;
}

export class OfficeWorkbenchIpcError extends Error {
  readonly code?: string;

  constructor(message: string, code?: string) {
    super(message);
    this.name = "OfficeWorkbenchIpcError";
    this.code = code;
  }
}

export function requireOfficeWorkbenchResult<T>(
  result: T | OfficeWorkbenchIpcFailure,
  fallbackMessage: string,
): T {
  if (
    result &&
    typeof result === "object" &&
    "success" in result &&
    result.success === false
  ) {
    throw new OfficeWorkbenchIpcError(
      typeof result.error === "string" && result.error.trim()
        ? result.error
        : fallbackMessage,
      typeof result.error_code === "string" ? result.error_code : undefined,
    );
  }
  return result as T;
}
