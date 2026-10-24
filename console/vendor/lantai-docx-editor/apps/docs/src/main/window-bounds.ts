export interface WindowWorkArea {
  x: number
  y: number
  width: number
  height: number
}

export interface DocsWindowBounds extends WindowWorkArea {}

const DOCS_WINDOW_WIDTH = 1360
const DOCS_WINDOW_HEIGHT = 900
const DOCS_WINDOW_MARGIN = 16

/** Keep the draggable ribbon row inside the display's usable desktop area. */
export function fitDocsWindowToWorkArea(workArea: WindowWorkArea): DocsWindowBounds {
  const width = Math.min(
    DOCS_WINDOW_WIDTH,
    Math.max(1, workArea.width - DOCS_WINDOW_MARGIN * 2),
  )
  const height = Math.min(
    DOCS_WINDOW_HEIGHT,
    Math.max(1, workArea.height - DOCS_WINDOW_MARGIN * 2),
  )
  return {
    x: workArea.x + Math.max(DOCS_WINDOW_MARGIN, Math.floor((workArea.width - width) / 2)),
    y: workArea.y + Math.max(DOCS_WINDOW_MARGIN, Math.floor((workArea.height - height) / 2)),
    width,
    height,
  }
}
