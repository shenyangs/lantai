import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { imageGenerationApi } from "@/api";
import type { ProviderInfo } from "@/api/types";
import { ImageGenerationSection } from "./ImageGenerationSection";

const success = vi.fn();
const error = vi.fn();
const info = vi.fn();

vi.mock("@/api", () => ({
  imageGenerationApi: {
    getImageGenerationConfig: vi.fn(),
    updateImageGenerationConfig: vi.fn(),
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

const getConfig = vi.mocked(imageGenerationApi.getImageGenerationConfig);
const updateConfig = vi.mocked(imageGenerationApi.updateImageGenerationConfig);

const providers = [
  {
    id: "image-provider",
    name: "Miyang",
    api_key_prefix: "",
    chat_model: "OpenAIChatModel",
    models: [
      {
        id: "image-v1",
        name: "Image V1",
        supports_multimodal: false,
        supports_image: true,
        supports_video: false,
        generate_kwargs: {},
      },
    ],
    extra_models: [],
    is_custom: true,
    is_local: false,
    support_model_discovery: true,
    support_connection_check: true,
    freeze_url: false,
    require_api_key: true,
    api_key: "configured",
    base_url: "https://images.example/v1",
    generate_kwargs: {},
  },
] satisfies ProviderInfo[];

describe("ImageGenerationSection", () => {
  beforeEach(() => {
    success.mockReset();
    error.mockReset();
    info.mockReset();
    getConfig.mockReset();
    updateConfig.mockReset();
    getConfig.mockResolvedValue({
      enabled: false,
      models: [],
      timeout_seconds: 120,
      works_cover_enabled: false,
      works_cover_model: { provider_id: "", model: "" },
    });
    updateConfig.mockImplementation(async (config) => config);
  });

  it("requires at least one dedicated model before enabling image generation", async () => {
    render(<ImageGenerationSection providers={providers} />);

    await waitFor(() => expect(getConfig).toHaveBeenCalledOnce());
    fireEvent.click(
      screen.getByRole("checkbox", {
        name: "models.imageGenerationEnabled",
      }),
    );

    expect(
      screen.getByText("models.imageGenerationNeedsModel"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "models.save" })).toBeDisabled();
  });

  it("adds an ordered model and saves the dedicated image configuration", async () => {
    render(<ImageGenerationSection providers={providers} />);

    await waitFor(() => expect(getConfig).toHaveBeenCalledOnce());
    fireEvent.change(
      screen.getAllByRole("combobox", {
        name: "models.selectProvider",
      })[0],
      { target: { value: "image-provider" } },
    );
    fireEvent.change(
      screen.getByRole("combobox", { name: "models.selectModel" }),
      { target: { value: "image-v1" } },
    );
    fireEvent.click(
      screen.getByRole("button", {
        name: "models.imageGenerationAdd",
      }),
    );
    fireEvent.click(
      screen.getByRole("checkbox", {
        name: "models.imageGenerationEnabled",
      }),
    );
    fireEvent.change(screen.getByRole("spinbutton", { name: "timeout" }), {
      target: { value: "90" },
    });
    fireEvent.click(screen.getByRole("button", { name: "models.save" }));

    await waitFor(() =>
      expect(updateConfig).toHaveBeenCalledWith({
        enabled: true,
        models: [{ provider_id: "image-provider", model: "image-v1" }],
        timeout_seconds: 90,
        works_cover_enabled: false,
        works_cover_model: { provider_id: "", model: "" },
      }),
    );
    expect(success).toHaveBeenCalledWith("models.imageGenerationUpdated");
  });
});
