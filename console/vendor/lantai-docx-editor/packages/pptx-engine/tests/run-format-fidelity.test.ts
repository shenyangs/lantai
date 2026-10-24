/**
 * Run-level format fidelity: latin/ea dual fonts kept + underline style not collapsed.
 * Modeled via parseSlide (with latinFont/eaFont/fontImplicit/underlineStyle markers),
 * then asserted at the byte level through patchTextElementXml.
 */
import { describe, it, expect } from 'vitest'
import { parseSlide } from '../src/parse'
import { patchTextElementXml, setElementFont } from '../src/index'
import type { TextElement } from '../src/types'

const slideWith = (sp: string) =>
  '<?xml version="1.0"?><p:sld xmlns:p="p" xmlns:a="a"><p:cSld>' +
  `<p:spTree><p:nvGrpSpPr/><p:grpSpPr/>${sp}</p:spTree></p:cSld></p:sld>`
const spWith = (runs: string) =>
  '<p:sp><p:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="100" cy="100"/></a:xfrm></p:spPr>' +
  `<p:txBody><a:bodyPr/><a:p>${runs}</a:p></p:txBody></p:sp>`

const parseEl = (runs: string, inheritedRunAttrs = '') => {
  const sp = inheritedRunAttrs
    ? spWith(runs).replace(
        '<a:bodyPr/>',
        `<a:bodyPr/><a:lstStyle><a:lvl1pPr><a:defRPr ${inheritedRunAttrs}/></a:lvl1pPr></a:lstStyle>`,
      )
    : spWith(runs)
  const slide = parseSlide({
    path: 'ppt/slides/slide1.xml',
    slideXml: slideWith(sp),
    ctx: {},
  })
  return { slide, el: slide.elements[0] as TextElement }
}

describe('bold/italic inheritance preserved', () => {
  it('text-only edit does not materialize inherited normal formatting', () => {
    const { el } = parseEl('<a:r><a:rPr sz="1800"/><a:t>x</a:t></a:r>')
    const original = el.anchor.originalXml
    const run = el.text!.paragraphs[0]!.runs[0]!
    expect(run.bold).toBe(false)
    expect(run.italic).toBe(false)
    run.text = 'y'
    expect(patchTextElementXml(el, original)).toBe(original.replace('<a:t>x</a:t>', '<a:t>y</a:t>'))
  })

  it('explicit normal formatting overrides inheritance even when the current display is already normal', () => {
    const { slide, el } = parseEl('<a:r><a:rPr sz="1800"/><a:t>x</a:t></a:r>')
    setElementFont(slide, el.id, { bold: false, italic: false })
    const out = patchTextElementXml(el, el.anchor.originalXml)
    expect(out).toContain(' b="0"')
    expect(out).toContain(' i="0"')
  })

  it('structural rebuild keeps bold/italic inherited from the list style', () => {
    const { el } = parseEl('<a:r><a:rPr sz="1800"/><a:t>x</a:t></a:r>', 'b="1" i="1"')
    const para = el.text!.paragraphs[0]!
    expect(para.runs[0]!.bold).toBe(true)
    expect(para.runs[0]!.italic).toBe(true)
    para.runs.push({ ...para.runs[0]!, text: 'y' })
    const out = patchTextElementXml(el, el.anchor.originalXml)
    expect(out).toContain('<a:defRPr b="1" i="1"/>')
    expect(out).not.toMatch(/<a:rPr\b[^>]*\s[bi]=/)
    const reparsed = parseSlide({
      path: 'ppt/slides/slide1.xml',
      slideXml: slideWith(out),
      ctx: {},
    })
    const restored = reparsed.elements[0] as TextElement
    expect(restored.text!.paragraphs[0]!.runs.map((r) => [r.bold, r.italic])).toEqual([
      [true, true],
      [true, true],
    ])
  })

  it('structural rebuild keeps the paragraph default run properties and their inheritance', () => {
    const defaults =
      '<a:defRPr b="1" i="1" lang="zh-CN"><a:solidFill><a:schemeClr val="accent1"/></a:solidFill><a:latin typeface="+mn-lt"/></a:defRPr>'
    const { el } = parseEl(
      `<a:pPr>${defaults}</a:pPr><a:r><a:rPr sz="1800"/><a:t>x</a:t></a:r>`,
    )
    const para = el.text!.paragraphs[0]!
    para.runs.push({ ...para.runs[0]!, text: 'y' })
    const out = patchTextElementXml(el, el.anchor.originalXml)
    expect(out).toContain(defaults)
    expect(out).not.toMatch(/<a:rPr\b[^>]*\s[bi]=/)
    const reparsed = parseSlide({
      path: 'ppt/slides/slide1.xml',
      slideXml: slideWith(out),
      ctx: {},
    })
    expect(
      (reparsed.elements[0] as TextElement).text!.paragraphs[0]!.runs.map((r) => [r.bold, r.italic]),
    ).toEqual([
      [true, true],
      [true, true],
    ])
  })

  it('text-only edit does not add an empty rPr when the original run had none', () => {
    const { el } = parseEl('<a:r><a:t>x</a:t></a:r>', 'b="1" i="1"')
    const original = el.anchor.originalXml
    el.text!.paragraphs[0]!.runs[0]!.text = 'y'
    expect(patchTextElementXml(el, original)).toBe(original.replace('<a:t>x</a:t>', '<a:t>y</a:t>'))
  })

  it('paragraph normal defaults override a bold/italic list style without changing text-only bytes', () => {
    const { el } = parseEl(
      '<a:pPr><a:defRPr b="0" i="0"/></a:pPr><a:r><a:t>x</a:t></a:r>',
      'b="1" i="1"',
    )
    const original = el.anchor.originalXml
    const run = el.text!.paragraphs[0]!.runs[0]!
    expect([run.bold, run.italic]).toEqual([false, false])
    run.text = 'y'
    expect(patchTextElementXml(el, original)).toBe(original.replace('<a:t>x</a:t>', '<a:t>y</a:t>'))
  })

  it('explicit normal formatting survives structural rebuild under bold/italic paragraph defaults', () => {
    const { slide, el } = parseEl(
      '<a:pPr><a:defRPr b="1" i="1"/></a:pPr><a:r><a:t>x</a:t></a:r>',
    )
    setElementFont(slide, el.id, { bold: false, italic: false })
    const para = el.text!.paragraphs[0]!
    para.runs.push({ ...para.runs[0]!, text: 'y' })
    const out = patchTextElementXml(el, el.anchor.originalXml)
    const reparsed = parseSlide({
      path: 'ppt/slides/slide1.xml',
      slideXml: slideWith(out),
      ctx: {},
    })
    expect(out).toContain('<a:defRPr b="1" i="1"/>')
    expect(
      (reparsed.elements[0] as TextElement).text!.paragraphs[0]!.runs.map((r) => [r.bold, r.italic]),
    ).toEqual([
      [false, false],
      [false, false],
    ])
  })

  it('reordered paragraphs carry their own default run formatting', () => {
    const { el } = parseEl(
      '<a:pPr><a:defRPr b="1" i="0"/></a:pPr><a:r><a:t>x</a:t></a:r></a:p>' +
        '<a:p><a:pPr><a:defRPr b="0" i="1"/></a:pPr><a:r><a:t>y</a:t></a:r>',
    )
    el.text!.paragraphs.reverse()
    const out = patchTextElementXml(el, el.anchor.originalXml)
    const reparsed = parseSlide({
      path: 'ppt/slides/slide1.xml',
      slideXml: slideWith(out),
      ctx: {},
    })
    expect(
      (reparsed.elements[0] as TextElement).text!.paragraphs.map((p) =>
        p.runs.map((r) => [r.text, r.bold, r.italic]),
      ),
    ).toEqual([
      [['y', false, true]],
      [['x', true, false]],
    ])
  })

  it('regrouping runs rebuilds paragraph boundaries even when the total run count is unchanged', () => {
    const { el } = parseEl(
      '<a:r><a:t>x</a:t></a:r><a:r><a:t>y</a:t></a:r></a:p>' +
        '<a:p><a:r><a:t>z</a:t></a:r>',
    )
    const [first, second] = el.text!.paragraphs
    second!.runs.unshift(first!.runs.pop()!)
    const out = patchTextElementXml(el, el.anchor.originalXml)
    const reparsed = parseSlide({
      path: 'ppt/slides/slide1.xml',
      slideXml: slideWith(out),
      ctx: {},
    })
    expect(
      (reparsed.elements[0] as TextElement).text!.paragraphs.map((p) =>
        p.runs.map((r) => r.text),
      ),
    ).toEqual([['x'], ['y', 'z']])
  })

  it.each(['b="1" i="1"', 'b="1" i="0"', 'b="0" i="1"', 'b="0" i="0"'])(
    'text-only edit retains explicit run attributes %s',
    (attrs) => {
      const { el } = parseEl(`<a:r><a:rPr ${attrs}/><a:t>x</a:t></a:r>`)
      const original = el.anchor.originalXml
      el.text!.paragraphs[0]!.runs[0]!.text = 'y'
      expect(patchTextElementXml(el, original)).toBe(
        original.replace('<a:t>x</a:t>', '<a:t>y</a:t>'),
      )
    },
  )

  it('an inherited run still follows later list-style changes after a text-only edit', () => {
    const { el } = parseEl('<a:r><a:rPr sz="1800"/><a:t>x</a:t></a:r>', 'b="1" i="1"')
    el.text!.paragraphs[0]!.runs[0]!.text = 'y'
    const out = patchTextElementXml(el, el.anchor.originalXml)
    const changedStyle = out.replace('<a:defRPr b="1" i="1"/>', '<a:defRPr b="0" i="0"/>')
    const reparsed = parseSlide({
      path: 'ppt/slides/slide1.xml',
      slideXml: slideWith(changedStyle),
      ctx: {},
    })
    const run = (reparsed.elements[0] as TextElement).text!.paragraphs[0]!.runs[0]!
    expect([run.bold, run.italic]).toEqual([false, false])
  })

  it('direct model formatting changes override inherited bold and italic', () => {
    const { el } = parseEl('<a:r><a:rPr sz="1800"/><a:t>x</a:t></a:r>', 'b="1" i="1"')
    const run = el.text!.paragraphs[0]!.runs[0]!
    run.bold = false
    run.italic = false
    const out = patchTextElementXml(el, el.anchor.originalXml)
    expect(out).toContain(' b="0"')
    expect(out).toContain(' i="0"')
  })

  it('explicit normal formatting survives structural rebuild under a bold/italic list style', () => {
    const { slide, el } = parseEl('<a:r><a:rPr sz="1800"/><a:t>x</a:t></a:r>', 'b="1" i="1"')
    setElementFont(slide, el.id, { bold: false, italic: false })
    const para = el.text!.paragraphs[0]!
    para.runs.push({ ...para.runs[0]!, text: 'y' })
    const out = patchTextElementXml(el, el.anchor.originalXml)
    const reparsed = parseSlide({
      path: 'ppt/slides/slide1.xml',
      slideXml: slideWith(out),
      ctx: {},
    })
    expect(
      (reparsed.elements[0] as TextElement).text!.paragraphs[0]!.runs.map((r) => [
        r.bold,
        r.italic,
      ]),
    ).toEqual([
      [false, false],
      [false, false],
    ])
  })
})

describe('latin/ea dual fonts preserved', () => {
  const DUAL =
    '<a:r><a:rPr lang="en" sz="1800"><a:latin typeface="Calibri"/><a:ea typeface="微软雅黑"/></a:rPr><a:t>混排 text</a:t></a:r>'

  it('parses latinFont/eaFont verbatim', () => {
    const { el } = parseEl(DUAL)
    const r = el.text!.paragraphs[0]!.runs[0]!
    expect(r.latinFont).toBe('Calibri')
    expect(r.eaFont).toBe('微软雅黑')
    expect(r.fontImplicit).toBeUndefined()
  })

  it('text-only edit: dual font declarations keep their original bytes', () => {
    const { el } = parseEl(DUAL)
    el.text!.paragraphs[0]!.runs[0]!.text = 'edited 编辑'
    const out = patchTextElementXml(el, el.anchor.originalXml)
    expect(out).toContain('<a:latin typeface="Calibri"/>')
    expect(out).toContain('<a:ea typeface="微软雅黑"/>')
    expect(out).toContain('edited 编辑')
  })

  it('theme font reference (+mn-lt) not materialized when the font is unchanged', () => {
    const { el } = parseEl('<a:r><a:rPr><a:latin typeface="+mn-lt"/></a:rPr><a:t>x</a:t></a:r>')
    el.text!.paragraphs[0]!.runs[0]!.text = 'y'
    const out = patchTextElementXml(el, el.anchor.originalXml)
    expect(out).toContain('typeface="+mn-lt"')
  })

  it('run without font declaration (inherited): text-only edit does not inject latin/ea', () => {
    const { el } = parseEl('<a:r><a:rPr sz="1800"/><a:t>x</a:t></a:r>')
    expect(el.text!.paragraphs[0]!.runs[0]!.fontImplicit).toBe(true)
    el.text!.paragraphs[0]!.runs[0]!.text = 'y'
    const out = patchTextElementXml(el, el.anchor.originalXml)
    expect(out).not.toContain('<a:latin')
  })

  it('setElementFont explicit font change: both latin/ea rewritten to the new font', () => {
    const { slide, el } = parseEl(DUAL)
    setElementFont(slide, el.id, { fontFamily: 'Arial' })
    const out = patchTextElementXml(el, el.anchor.originalXml)
    expect(out).toContain('<a:latin typeface="Arial"/>')
    expect(out).toContain('<a:ea typeface="Arial"/>')
    expect(out).not.toContain('微软雅黑')
  })
})

describe('underline style not collapsed', () => {
  it('u="dbl" stays dbl after a text-only edit', () => {
    const { el } = parseEl('<a:r><a:rPr u="dbl"/><a:t>x</a:t></a:r>')
    const r = el.text!.paragraphs[0]!.runs[0]!
    expect(r.underline).toBe(true)
    expect(r.underlineStyle).toBe('dbl')
    r.text = 'y'
    const out = patchTextElementXml(el, el.anchor.originalXml)
    expect(out).toContain('u="dbl"')
    expect(out).not.toContain('u="sng"')
  })

  it('removing underline: existing u becomes none; runs without u get nothing injected', () => {
    const { el } = parseEl(
      '<a:r><a:rPr u="wavy"/><a:t>a</a:t></a:r><a:r><a:rPr/><a:t>b</a:t></a:r>',
    )
    const [r1, r2] = el.text!.paragraphs[0]!.runs
    r1!.underline = false
    delete r1!.underlineStyle
    r2!.text = 'b2'
    const out = patchTextElementXml(el, el.anchor.originalXml)
    expect(out).toContain('u="none"')
    expect(out).not.toContain('u="wavy"')
    // The second run had no u originally; do not inject one
    expect(out.match(/\su="/g)!.length).toBe(1)
  })

  it('newly enabled underline writes sng', () => {
    const { el } = parseEl('<a:r><a:rPr/><a:t>x</a:t></a:r>')
    el.text!.paragraphs[0]!.runs[0]!.underline = true
    const out = patchTextElementXml(el, el.anchor.originalXml)
    expect(out).toContain('u="sng"')
  })
})
