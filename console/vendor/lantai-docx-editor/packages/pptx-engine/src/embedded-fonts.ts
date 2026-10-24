import type { PackageArchive } from './zip'

function replaceText(
  archive: PackageArchive,
  path: string,
  transform: (xml: string) => string,
): boolean {
  const xml = archive.readText(path)
  if (!xml) return false
  const next = transform(xml)
  if (next === xml) return false
  archive.entries.set(path, Buffer.from(next, 'utf8'))
  return true
}

/**
 * Embedded PPTX fonts are subsetted to the original glyphs. Once editable
 * content changes, retaining that subset can make new characters disappear.
 */
export function stripEmbeddedFonts(archive: PackageArchive): boolean {
  let changed = false
  for (const path of [...archive.entries.keys()]) {
    if (!path.startsWith('ppt/fonts/')) continue
    archive.entries.delete(path)
    changed = true
  }

  changed =
    replaceText(archive, 'ppt/presentation.xml', (xml) =>
      xml.replace(/<p:embeddedFontLst\b[^>]*>[\s\S]*?<\/p:embeddedFontLst>/g, ''),
    ) || changed
  changed =
    replaceText(archive, 'ppt/_rels/presentation.xml.rels', (xml) =>
      xml.replace(
        /<Relationship\b(?=[^>]*\bType=["'][^"']*\/relationships\/font["'])[^>]*\/?>(?:\s*<\/Relationship>)?/g,
        '',
      ),
    ) || changed
  changed =
    replaceText(archive, '[Content_Types].xml', (xml) =>
      xml
        .replace(
          /<Override\b(?=[^>]*\bPartName=["']\/ppt\/fonts\/[^"']+["'])[^>]*\/?>(?:\s*<\/Override>)?/g,
          '',
        )
        .replace(
          /<Default\b(?=[^>]*\bExtension=["'](?:fntdata|odttf)["'])[^>]*\/?>(?:\s*<\/Default>)?/g,
          '',
        ),
    ) || changed
  return changed
}
