type MCPTransport = "stdio" | "streamable_http" | "sse";

function normalizeTransport(raw?: unknown): MCPTransport | undefined {
  if (typeof raw !== "string") return undefined;
  const value = raw.trim().toLowerCase();
  switch (value) {
    case "stdio":
      return "stdio";
    case "sse":
      return "sse";
    case "streamablehttp":
    case "streamable_http":
    case "streamable-http":
    case "http":
      return "streamable_http";
    default:
      return undefined;
  }
}

export function normalizeImportedClientData(key: string, rawData: any) {
  const transport =
    normalizeTransport(rawData.transport ?? rawData.type) ??
    (rawData.url || rawData.baseUrl || !rawData.command
      ? "streamable_http"
      : "stdio");

  const command =
    transport === "stdio" ? (rawData.command ?? "").toString() : "";

  return {
    name: rawData.name || key,
    description: rawData.description || "",
    // Interactive imports are review-first. Imported activation flags must not
    // turn a newly added server on before the user has inspected it.
    enabled: false,
    transport,
    url: (rawData.url || rawData.baseUrl || "").toString(),
    headers: rawData.headers || {},
    command,
    args: Array.isArray(rawData.args) ? rawData.args : [],
    env: rawData.env || {},
    cwd: (rawData.cwd || "").toString(),
    oauth: rawData.oauth || {},
    allowed_tools: Array.isArray(rawData.allowed_tools)
      ? rawData.allowed_tools
      : [],
  };
}

export function normalizeImportedClients(parsed: any) {
  const clientsToCreate: Array<{ key: string; data: any }> = [];

  if (parsed?.mcpServers && typeof parsed.mcpServers === "object") {
    Object.entries(parsed.mcpServers).forEach(
      ([key, data]: [string, any]) => {
        clientsToCreate.push({
          key,
          data: normalizeImportedClientData(key, data),
        });
      },
    );
  } else if (
    parsed?.key &&
    (parsed.command || parsed.url || parsed.baseUrl)
  ) {
    const { key, ...clientData } = parsed;
    clientsToCreate.push({
      key,
      data: normalizeImportedClientData(key, clientData),
    });
  } else if (parsed && typeof parsed === "object") {
    Object.entries(parsed).forEach(([key, data]: [string, any]) => {
      if (
        data &&
        typeof data === "object" &&
        (data.command || data.url || data.baseUrl)
      ) {
        clientsToCreate.push({
          key,
          data: normalizeImportedClientData(key, data),
        });
      }
    });
  }

  return clientsToCreate;
}
