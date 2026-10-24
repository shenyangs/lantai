import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import api from "../../../../../api";
import { ModelCompatibilityPanel } from "./ModelCompatibilityPanel";

vi.mock("../../../../../api", () => ({
  default: {
    checkModelCompatibility: vi.fn(),
  },
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string) => key,
  }),
}));

describe("ModelCompatibilityPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.checkModelCompatibility).mockResolvedValue({
      schema_version: 1,
      provider_id: "example",
      model_id: "model-a",
      status: "partial",
      checks: [
        {
          id: "text_stream",
          status: "passed",
          detail_code: "append_only_complete",
          event_count: 2,
        },
        {
          id: "tool_call",
          status: "failed",
          detail_code: "not_returned",
          event_count: 0,
        },
      ],
      duration_ms: 42,
      event_counts: { text_delta: 2, message_completed: 1 },
      trace_digest: "a".repeat(64),
      data_classification: "structure_only",
    });
  });

  it("discloses cost and privacy before starting the probe", () => {
    render(<ModelCompatibilityPanel providerId="example" modelId="model-a" />);

    expect(
      screen.getByText("models.compatibilityProbeDisclosure"),
    ).toBeInTheDocument();
    expect(api.checkModelCompatibility).not.toHaveBeenCalled();
  });

  it("shows a localized structural report for the selected model", async () => {
    render(<ModelCompatibilityPanel providerId="example" modelId="model-a" />);

    fireEvent.click(
      screen.getByRole("button", { name: "models.runCompatibilityProbe" }),
    );

    await waitFor(() =>
      expect(api.checkModelCompatibility).toHaveBeenCalledWith("example", {
        model_id: "model-a",
      }),
    );
    expect(
      await screen.findByText("models.compatibilityStatus.partial"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("models.compatibilityChecks.text_stream"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("models.compatibilityChecks.tool_call"),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("brief private reasoning"),
    ).not.toBeInTheDocument();
  });
});
