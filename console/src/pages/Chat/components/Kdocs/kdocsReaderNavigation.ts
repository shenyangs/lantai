/** Text offsets refer to rendered text in one immutable reading snapshot. */
export function findTextMatches(
  text: string,
  query: string,
): Array<[number, number]> {
  if (!query.trim()) return [];
  const matches: Array<[number, number]> = [];
  let cursor = 0;
  const source = text.toLocaleLowerCase();
  const needle = query.toLocaleLowerCase();
  while (cursor <= source.length - needle.length) {
    const start = source.indexOf(needle, cursor);
    if (start < 0) break;
    matches.push([start, start + needle.length]);
    cursor = start + needle.length;
  }
  return matches;
}

export function textRange(
  root: HTMLElement,
  start: number,
  end: number,
): Range | null {
  const walker = root.ownerDocument.createTreeWalker(
    root,
    NodeFilter.SHOW_TEXT,
  );
  const range = root.ownerDocument.createRange();
  let offset = 0;
  let began = false;
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const length = node.textContent?.length || 0;
    if (!began && start < offset + length) {
      range.setStart(node, start - offset);
      began = true;
    }
    if (began && end <= offset + length) {
      range.setEnd(node, end - offset);
      return range;
    }
    offset += length;
  }
  return null;
}
