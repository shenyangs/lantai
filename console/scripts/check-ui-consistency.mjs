import fs from "node:fs";
import path from "node:path";
import less from "less";
import ts from "typescript";

const projectRoot = path.resolve(import.meta.dirname, "..");
const sourceRoot = path.join(projectRoot, "src");
const failures = [];

function walk(directory) {
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const absolute = path.join(directory, entry.name);
    if (entry.isDirectory()) return walk(absolute);
    if (!entry.isFile() || !/\.tsx$/.test(entry.name)) return [];
    if (/\.(test|spec)\.tsx$/.test(entry.name)) return [];
    return [absolute];
  });
}

function walkStyleModules(directory) {
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const absolute = path.join(directory, entry.name);
    if (entry.isDirectory()) return walkStyleModules(absolute);
    if (!entry.isFile() || !/\.module\.less$/.test(entry.name)) return [];
    return [absolute];
  });
}

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function cssModuleBindings(source) {
  const bindings = new Map();
  for (const statement of source.statements) {
    if (!ts.isImportDeclaration(statement)) continue;
    const specifier = statement.moduleSpecifier;
    const defaultImport = statement.importClause?.name;
    if (
      !defaultImport ||
      !ts.isStringLiteral(specifier) ||
      !specifier.text.endsWith(".module.less")
    ) {
      continue;
    }
    bindings.set(defaultImport.text, specifier.text);
  }
  return bindings;
}

function styleClasses(expression, source, binding) {
  if (!expression) return [];
  const pattern = new RegExp(`${escapeRegExp(binding)}\\.([A-Za-z0-9_]+)`, "g");
  return [...expression.getText(source).matchAll(pattern)].map(
    (match) => match[1],
  );
}

function hasButtonDescendant(node, source) {
  let found = false;
  function visit(child) {
    if (found) return;
    if (
      child !== node &&
      (ts.isJsxElement(child) || ts.isJsxSelfClosingElement(child))
    ) {
      const opening = ts.isJsxElement(child) ? child.openingElement : child;
      if (opening.tagName.getText(source) === "Button") {
        found = true;
        return;
      }
    }
    ts.forEachChild(child, visit);
  }
  ts.forEachChild(node, visit);
  return found;
}

function collectButtonContainerStyles(file) {
  const text = fs.readFileSync(file, "utf8");
  const source = ts.createSourceFile(
    file,
    text,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TSX,
  );
  const bindings = cssModuleBindings(source);
  const containers = [];

  function visit(node) {
    if (ts.isJsxElement(node) && hasButtonDescendant(node, source)) {
      const className = node.openingElement.attributes.properties.find(
        (attribute) =>
          ts.isJsxAttribute(attribute) && attribute.name.text === "className",
      );
      if (className && ts.isJsxAttribute(className) && className.initializer) {
        for (const [binding, specifier] of bindings) {
          for (const name of styleClasses(
            className.initializer,
            source,
            binding,
          )) {
            containers.push({
              file,
              line:
                source.getLineAndCharacterOfPosition(node.getStart(source))
                  .line + 1,
              name,
              styleFile: path.resolve(path.dirname(file), specifier),
            });
          }
        }
      }
    }
    ts.forEachChild(node, visit);
  }

  visit(source);
  return containers;
}

async function checkButtonTextColorScopes(files) {
  const containers = files.flatMap(collectButtonContainerStyles);
  const relevantStyles = new Set(containers.map((item) => item.styleFile));
  const renderedStyles = new Map();

  for (const styleFile of walkStyleModules(sourceRoot)) {
    if (!relevantStyles.has(styleFile)) continue;
    const result = await less.render(fs.readFileSync(styleFile, "utf8"), {
      filename: styleFile,
    });
    renderedStyles.set(styleFile, result.css);
  }

  for (const container of containers) {
    const css = renderedStyles.get(container.styleFile);
    if (!css) continue;
    const unsafeSelector = new RegExp(
      `\\.${escapeRegExp(container.name)}\\s+span(?=[\\s:{\\[])`,
    );
    const hasUnsafeColorRule = [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)].some(
      ([, selector, declarations]) =>
        unsafeSelector.test(selector) && /\bcolor\s*:/.test(declarations),
    );
    if (!hasUnsafeColorRule) continue;
    failures.push(
      `${path.relative(projectRoot, container.file)}:${container.line} ` +
        `.${container.name} colors every descendant <span> even though it contains a Button; ` +
        "scope the text selector to the intended copy element",
    );
  }
}

function attributeNames(attributes) {
  return new Set(
    attributes.properties
      .filter(ts.isJsxAttribute)
      .map((attribute) => attribute.name.text),
  );
}

function checkInteractiveSemantics(file) {
  const text = fs.readFileSync(file, "utf8");
  const source = ts.createSourceFile(
    file,
    text,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TSX,
  );

  function visit(node) {
    if (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) {
      const tag = node.tagName.getText(source);
      if (tag === "div" || tag === "span") {
        const names = attributeNames(node.attributes);
        const isClickTarget = names.has("onClick");
        const isEventBoundary = names.has("data-event-boundary");
        const isKeyboardReady =
          names.has("role") && names.has("tabIndex") && names.has("onKeyDown");
        if (isClickTarget && !isEventBoundary && !isKeyboardReady) {
          const { line, character } = source.getLineAndCharacterOfPosition(
            node.getStart(source),
          );
          failures.push(
            `${path.relative(projectRoot, file)}:${line + 1}:${character + 1} ` +
              `<${tag}> has onClick without role, tabIndex and onKeyDown`,
          );
        }
      }
    }
    ts.forEachChild(node, visit);
  }

  visit(source);
}

const sourceFiles = walk(sourceRoot);
for (const file of sourceFiles) checkInteractiveSemantics(file);
await checkButtonTextColorScopes(sourceFiles);

const tokens = fs.readFileSync(
  path.join(sourceRoot, "styles/base/tokens.css"),
  "utf8",
);
for (const token of [
  "--lt-type-page-title",
  "--lt-space-4",
  "--lt-control-height",
  "--lt-layer-dialog",
]) {
  if (!tokens.includes(token)) failures.push(`Missing semantic token ${token}`);
}

const detailNavigation = fs.readFileSync(
  path.join(sourceRoot, "navigation/detailNavigation.ts"),
  "utf8",
);
for (const primaryPath of ["/tools", "/channels", "/cron-jobs", "/memory"]) {
  if (detailNavigation.includes(`"${primaryPath}":`)) {
    failures.push(
      `${primaryPath} must not be treated as a settings detail route`,
    );
  }
}

if (failures.length > 0) {
  console.error("UI consistency check failed:\n");
  for (const failure of failures) console.error(`- ${failure}`);
  process.exitCode = 1;
} else {
  console.log("UI consistency check passed.");
}
