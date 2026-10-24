import { expect, it } from "vitest";

it("retains DOM matchers, storage, and component platform shims", () => {
  const element = document.createElement("div");
  document.body.append(element);
  expect(element).toBeInTheDocument();
  localStorage.setItem("environment-contract", "ok");
  expect(window.localStorage.getItem("environment-contract")).toBe("ok");
  expect(window.matchMedia("(min-width: 1px)").matches).toBe(false);
  expect(new ResizeObserver(() => {})).toBeDefined();
  element.remove();
  localStorage.clear();
});
