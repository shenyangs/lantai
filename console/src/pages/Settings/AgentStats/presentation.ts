export function toolSuccessRateLabel(
  totalCalls: number,
  successRate: number,
): string {
  if (totalCalls <= 0) return "—";
  const normalizedRate = Number.isFinite(successRate)
    ? Math.max(0, Math.min(100, successRate))
    : 0;
  return `${normalizedRate}%`;
}
