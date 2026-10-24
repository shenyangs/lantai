export function isPlanCommand(value: string): boolean {
  return value.trim().toLowerCase().startsWith("/plan ");
}

export function buildPlanPrompt(value: string): string {
  const text = value.trim();
  if (!text || isPlanCommand(text)) return text;
  return `/plan ${text}`;
}
