import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { videoGenerationApi } from "@/api";
import type { ProviderInfo } from "@/api/types";
import { VideoGenerationSection } from "./VideoGenerationSection";

const success = vi.fn();
const error = vi.fn();
const info = vi.fn();

vi.mock("@/api", () => ({
  videoGenerationApi: {
    getVideoGenerationConfig: vi.fn(),
    updateVideoGenerationConfig: vi.fn(),
  },
}));

vi.mock("@/hooks/useAppMessage", () => ({
  useAppMessage: () => ({
    message: { success, error, info },
  }),
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string) => key,
  }),
}));

vi.mock("@/vendor/lantaiDesign", () => ({
  Button: ({
    children,
    disabled,
    loading,
    onClick,
    "aria-label": ariaLabel,
  }: {
    children?: React.ReactNode;
    disabled?: boolean;
    loading?: boolean;
    onClick?: () => void;
    "aria-label"?: string;
  }) => (
    <button
      type="button"
      aria-label={ariaLabel}
      disabled={disabled || loading}
      onClick={onClick}
    >
      {children}
    </button>
  ),
  Card: ({
    children,
    title,
    extra,
  }: {
    children?: React.ReactNode;
    title?: React.ReactNode;
    extra?: React.ReactNode;
  }) => (
    <section>
      <h2>{title}</h2>
      {extra}
      {children}
    </section>
  ),
  InputNumber: ({
    value,
    disabled,
    onChange,
  }: {
    value?: number;
    disabled?: boolean;
    onChange?: (value: number | null) => void;
  }) => (
    <input
      aria-label="timeout"
      type="number"
      value={value}
      disabled={disabled}
      onChange={(event) => onChange?.(Number(event.target.value))}
    />
  ),
  Select: ({
    value,
    disabled,
    options = [],
    placeholder,
    onChange,
  }: {
    value?: string;
    disabled?: boolean;
    options?: Array<{ value: string; label: string }>;
    placeholder?: React.ReactNode;
    onChange?: (value: string) => void;
  }) => (
    <select
      aria-label={String(placeholder ?? "")}
      value={value ?? ""}
      disabled={disabled}
      onChange={(event) => onChange?.(event.target.value)}
    >
      <option value="">{placeholder}</option>
      {options.map((option) => (
        <option key={option.value} value={option.value}>
          {option.label}
        </option>
      ))}
    </select>
  ),
  Switch: ({
    checked,
    disabled,
    onChange,
    "aria-label": ariaLabel,
  }: {
    checked?: boolean;
    disabled?: boolean;
    onChange?: (checked: boolean) => void;
    "aria-label"?: string;
  }) => (
    <input
      type="checkbox"
      aria-label={ariaLabel}
      checked={checked}
      disabled={disabled}
      onChange={(event) => onChange?.(event.target.checked)}
    />
  ),
  Tooltip: ({ children }: { children?: React.ReactNode }) => children,
}));

vi.mock("@dnd-kit/core", () => ({
  DndContext: ({ children }: { children?: React.ReactNode }) => children,
  KeyboardSensor: class {},
  PointerSensor: class {},
  closestCenter: vi.fn(),
  useSensor: vi.fn(() => ({})),
  useSensors: vi.fn(() => []),
}));

vi.mock("@dnd-kit/sortable", () => ({
  SortableContext: ({ children }: { children?: React.ReactNode }) => children,
  arrayMove: (items: unknown[], from: number, to: number) => {
    const next = [...items];
    const [item] = next.splice(from, 1);
    next.splice(to, 0, item);
    return next;
  },
  sortableKeyboardCoordinates: vi.fn(),
  useSortable: () => ({
    attributes: {},
    listeners: {},
    setNodeRef: vi.fn(),
    transform: null,
    transition: undefined,
    isDragging: false,
  }),
  verticalListSortingStrategy: {},
}));

vi.mock("@dnd-kit/utilities", () => ({
  CSS: { Transform: { toString: () => undefined } },
}));

const getConfig = vi.mocked(videoGenerationApi.getVideoGenerationConfig);
const updateConfig = vi.mocked(videoGenerationApi.updateVideoGenerationConfig);

const providers = [
  {
    id: "tokendance",
    name: "TokenDance",
    api_key_prefix: "",
    chat_model: "OpenAIChatModel",
    models: [
      {
        id: "seedance-2.0",
        name: "Seedance 2.0",
        supports_multimodal: false,
        supports_image: false,
        supports_video: true,
        supported_protocols: ["seedance:generations"],
        generate_kwargs: {},
      },
      {
        id: "minimax-h3",
        name: "MiniMax H3",
        supports_multimodal: false,
        supports_image: false,
        supports_video: true,
        supported_protocols: ["minimax:video_generation_v2"],
        generate_kwargs: {},
      },
    ],
    extra_models: [],
    is_custom: false,
    is_local: false,
    support_model_discovery: true,
    support_connection_check: true,
    freeze_url: true,
    require_api_key: true,
    api_key: "configured",
    base_url: "https://tokendance.example",
    generate_kwargs: {},
    supports_video_generation: true,
  },
] satisfies ProviderInfo[];

describe("VideoGenerationSection", () => {
  beforeEach(() => {
    success.mockReset();
    error.mockReset();
    info.mockReset();
    getConfig.mockReset();
    updateConfig.mockReset();
    getConfig.mockResolvedValue({
      enabled: false,
      models: [],
      timeout_seconds: 600,
    });
    updateConfig.mockImplementation(async (config) => config);
  });

  it("requires a text-to-video route before enabling", async () => {
    render(<VideoGenerationSection providers={providers} />);

    await waitFor(() => expect(getConfig).toHaveBeenCalledOnce());
    fireEvent.click(
      screen.getByRole("checkbox", {
        name: "models.videoGenerationEnabled",
      }),
    );

    expect(screen.getByText("models.videoGenerationNeedsModel")).toBeVisible();
    expect(screen.getByRole("button", { name: "models.save" })).toBeDisabled();
  });

  it("adds Seedance to an ordered queue and saves the longer timeout", async () => {
    render(<VideoGenerationSection providers={providers} />);

    await waitFor(() => expect(getConfig).toHaveBeenCalledOnce());
    fireEvent.change(
      screen.getAllByRole("combobox", {
        name: "models.selectProvider",
      })[0],
      { target: { value: "tokendance" } },
    );
    fireEvent.change(
      screen.getByRole("combobox", { name: "models.selectModel" }),
      { target: { value: "seedance-2.0" } },
    );
    fireEvent.click(
      screen.getByRole("button", { name: "models.videoGenerationAdd" }),
    );
    fireEvent.click(
      screen.getByRole("checkbox", {
        name: "models.videoGenerationEnabled",
      }),
    );
    fireEvent.change(screen.getByRole("spinbutton", { name: "timeout" }), {
      target: { value: "900" },
    });
    fireEvent.click(screen.getByRole("button", { name: "models.save" }));

    await waitFor(() =>
      expect(updateConfig).toHaveBeenCalledWith({
        enabled: true,
        models: [{ provider_id: "tokendance", model: "seedance-2.0" }],
        timeout_seconds: 900,
      }),
    );
    expect(success).toHaveBeenCalledWith("models.videoGenerationUpdated");
  });

  it("locks every editor while a save snapshot is in flight", async () => {
    let resolveSave:
      | ((value: {
          enabled: boolean;
          models: Array<{ provider_id: string; model: string }>;
          timeout_seconds: number;
        }) => void)
      | undefined;
    updateConfig.mockImplementation(
      (_config) =>
        new Promise((resolve) => {
          resolveSave = resolve;
        }),
    );
    render(<VideoGenerationSection providers={providers} />);

    await waitFor(() => expect(getConfig).toHaveBeenCalledOnce());
    fireEvent.change(
      screen.getAllByRole("combobox", {
        name: "models.selectProvider",
      })[0],
      { target: { value: "tokendance" } },
    );
    fireEvent.change(
      screen.getByRole("combobox", { name: "models.selectModel" }),
      { target: { value: "seedance-2.0" } },
    );
    fireEvent.click(
      screen.getByRole("button", { name: "models.videoGenerationAdd" }),
    );
    fireEvent.click(
      screen.getByRole("checkbox", {
        name: "models.videoGenerationEnabled",
      }),
    );
    fireEvent.change(screen.getByRole("spinbutton", { name: "timeout" }), {
      target: { value: "900" },
    });
    fireEvent.click(screen.getByRole("button", { name: "models.save" }));

    expect(
      screen.getByRole("checkbox", {
        name: "models.videoGenerationEnabled",
      }),
    ).toBeDisabled();
    expect(screen.getAllByRole("combobox")[0]).toBeDisabled();
    expect(screen.getAllByRole("combobox")[1]).toBeDisabled();
    expect(screen.getByRole("spinbutton", { name: "timeout" })).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "models.videoGenerationAdd" }),
    ).toBeDisabled();

    fireEvent.change(screen.getByRole("spinbutton", { name: "timeout" }), {
      target: { value: "300" },
    });
    resolveSave?.({
      enabled: true,
      models: [{ provider_id: "tokendance", model: "seedance-2.0" }],
      timeout_seconds: 900,
    });

    await waitFor(() => expect(success).toHaveBeenCalledOnce());
    expect(screen.getByRole("spinbutton", { name: "timeout" })).toHaveValue(
      900,
    );
  });

  it("shows only providers whose backend contract exposes video generation", async () => {
    const catalogOnlyProvider = {
      ...providers[0],
      id: "catalog-only",
      name: "Catalog only",
      supports_video_generation: false,
    } satisfies ProviderInfo;

    render(
      <VideoGenerationSection
        providers={[...providers, catalogOnlyProvider]}
      />,
    );

    await waitFor(() => expect(getConfig).toHaveBeenCalledOnce());
    const providerSelect = screen.getAllByRole("combobox", {
      name: "models.selectProvider",
    })[0];
    expect(providerSelect).toHaveTextContent("TokenDance");
    expect(providerSelect).not.toHaveTextContent("Catalog only");
  });
});
