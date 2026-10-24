const GENOFFICE_SOURCE_PIN_PATTERN = /固定提交(?:：|仍为\s+)`([0-9a-f]{40})`/u;

export function parseGenOfficeSourcePin(source) {
  return source.match(GENOFFICE_SOURCE_PIN_PATTERN)?.[1];
}
