import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const runtimeRoot = fileURLToPath(new URL("..", import.meta.url));
const editors = ["docs", "sheets", "slides", "pdf"];
const failures = [];

for (const editor of editors) {
  const rendererRoot = join(runtimeRoot, "apps", editor, "src", "renderer");
  const stylesPath = join(rendererRoot, "styles.css");
  const mainPath = join(rendererRoot, "main.tsx");
  const styles = readFileSync(stylesPath, "utf8");
  const main = readFileSync(mainPath, "utf8");

  if (!styles.includes("[data-theme='dark']")) {
    failures.push(`${editor}: styles.css has no explicit dark-theme tokens`);
  }
  if (!styles.includes("prefers-color-scheme: dark")) {
    failures.push(`${editor}: styles.css does not follow the system dark theme`);
  }
  if (!/removeAttribute\(['\"]data-theme['\"]\)/.test(main)) {
    failures.push(`${editor}: system theme does not clear the explicit override`);
  }
  if (!/setAttribute\(['\"]data-theme['\"],\s*theme\)/.test(main)) {
    failures.push(`${editor}: explicit light/dark theme is not applied`);
  }
}

if (failures.length > 0) {
  console.error("Office editor theme contract failed:");
  for (const failure of failures) console.error(`- ${failure}`);
  process.exit(1);
}

console.log(
  `Office editor theme contract passed for ${editors.length} editors.`,
);
