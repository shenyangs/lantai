// PptxGenJS backend for Lantai's generic native PPTX compiler.
//
// This is the only presentation module allowed to depend on the PptxGenJS
// API. It maps an already-composed, explicitly framed scene to editable
// native objects. It does not choose layouts or interpret profile identities.

import fs from "node:fs";
import { createRequire } from "node:module";

const SLIDE_WIDTH = 13.333;
const SLIDE_HEIGHT = 7.5;

export function chartColorsFor({ isPie, seriesCount, palette }) {
  if (!isPie && seriesCount === 1) return palette.slice(0, 1);
  return palette;
}

export async function compileWithPptxGenJS({ deck, outputPath, modulePath }) {
const require = createRequire(import.meta.url);
const PptxGenJS = require(modulePath);
const pptx = new PptxGenJS();
const shapeType = pptx.ShapeType;
const chartType = pptx.ChartType;
const degradations = [];
let editableObjectCount = 0;

function cleanHex(value, fallback) {
  const normalized = String(value || "")
    .replace(/^#/, "")
    .toUpperCase();
  return /^[0-9A-F]{6}$/.test(normalized) ? normalized : fallback;
}

function numeric(value, fallback) {
  if (value === null || value === undefined) return fallback;
  return Number.isFinite(Number(value)) ? Number(value) : fallback;
}

function clamp(value, minimum, maximum) {
  return Math.min(maximum, Math.max(minimum, numeric(value, minimum)));
}

const executionTokens = deck.composition_tokens || {};
const providedChartSeries = Array.isArray(executionTokens.chart_series)
  ? executionTokens.chart_series
      .map((value) => cleanHex(value, ""))
      .filter((value) => Boolean(value))
  : [];
const tokens = {
  canvas: cleanHex(executionTokens.canvas, "F7F7F4"),
  ink: cleanHex(executionTokens.ink, "171717"),
  muted: cleanHex(executionTokens.muted, "666666"),
  accent: cleanHex(executionTokens.accent, "D9583B"),
  subtle: cleanHex(executionTokens.subtle, "E5E5E0"),
  titleFont: String(executionTokens.title_font || "Noto Sans SC"),
  bodyFont: String(executionTokens.body_font || "Noto Sans SC"),
  titleSize: clamp(executionTokens.title_size, 24, 72),
  bodySize: clamp(executionTokens.body_size, 12, 36),
  captionSize: clamp(executionTokens.caption_size, 9, 24),
  chartSeries:
    providedChartSeries.length > 0
      ? providedChartSeries
      : ["D9583B", "171717", "666666", "E5E5E0"],
};

pptx.layout = "LAYOUT_WIDE";
pptx.author = "Lantai Presentation Skill v3";
pptx.company = "Lantai";
pptx.subject = String(deck.title || "Presentation");
pptx.title = String(deck.title || "Presentation");
pptx.lang = String(deck.language || "zh-CN");
pptx.theme = {
  headFontFace: tokens.titleFont,
  bodyFontFace: tokens.bodyFont,
  lang: String(deck.language || "zh-CN"),
};

function frame(value) {
  if (!value) return null;
  const result = {
    x: numeric(value.x, 0),
    y: numeric(value.y, 0),
    w: numeric(value.w, 0),
    h: numeric(value.h, 0),
  };
  const onCanvas =
    result.x >= 0 &&
    result.y >= 0 &&
    result.w > 0 &&
    result.h >= 0 &&
    result.x + result.w <= SLIDE_WIDTH + 0.001 &&
    result.y + result.h <= SLIDE_HEIGHT + 0.001;
  return onCanvas ? result : null;
}

function objectFrame(slideSpec, object) {
  const explicit = frame(object.frame);
  if (explicit) return explicit;
  throw new Error(
    `${slideSpec.id}:${object.id}: native backend requires an explicit valid frame`,
  );
}

function fontSizeForText(object) {
  const role = String(object.role || "body");
  const requested = numeric(object.font_size, NaN);
  if (Number.isFinite(requested)) return requested;
  if (role === "title") return tokens.titleSize;
  if (role === "caption" || role === "meta" || role === "attribution") {
    return tokens.captionSize;
  }
  return tokens.bodySize;
}

function richTextRuns(object, baseSize, baseColor) {
  if (!Array.isArray(object.runs) || object.runs.length === 0) {
    return String(object.text ?? "");
  }
  return object.runs.map((run) => ({
    text: run.paragraph_break && ["\n", "\v"].includes(run.text) ? "" : String(run.text ?? ""),
    options: {
      bold: Boolean(run.bold),
      italic: Boolean(run.italic),
      color: cleanHex(run.color, baseColor),
      fontSize: numeric(run.font_size, baseSize),
      breakLine: Boolean(run.paragraph_break && ["\n", "\v"].includes(run.text)),
    },
  }));
}

function addTextObject(slide, object, box) {
  const size = fontSizeForText(object);
  const color = cleanHex(object.color, tokens.ink);
  const role = String(object.role || "body");
  const hasRuns = Array.isArray(object.runs) && object.runs.length > 0;
  slide.addText(richTextRuns(object, size, color), {
    ...box,
    objectName: object.id,
    fontFace: role === "title" ? tokens.titleFont : tokens.bodyFont,
    fontSize: size,
    color,
    bold: Boolean(
      !hasRuns && (object.emphasis || role === "title" || role === "heading"),
    ),
    // PptxGenJS fills false run options from textbox defaults. Keep defaults
    // neutral when inline formatting is authoritative, including regular text.
    italic: !hasRuns && role === "quote",
    align: object.align || "left",
    valign: object.valign || "top",
    margin: 0,
    fit: "shrink",
    breakLine: false,
  });
  editableObjectCount += 1;
}

function addMetricObject(slide, object, box) {
  const runs = [
    {
      text: String(object.value ?? ""),
      options: {
        bold: true,
        fontSize: clamp(Math.min(box.h * 28, box.w * 17), 22, 54),
        color: tokens.accent,
        breakLine: true,
      },
    },
    {
      text: String(object.label ?? ""),
      options: {
        bold: true,
        fontSize: tokens.bodySize,
        color: tokens.ink,
        breakLine: Boolean(object.detail),
      },
    },
  ];
  if (object.detail) {
    runs.push({
      text: String(object.detail),
      options: { fontSize: tokens.captionSize, color: tokens.muted },
    });
  }
  slide.addText(runs, {
    ...box,
    objectName: object.id,
    fontFace: tokens.bodyFont,
    margin: 0,
    fit: "shrink",
    valign: "mid",
  });
  editableObjectCount += 1;
}

function addImageObject(slide, object, box) {
  if (!object.path || !fs.existsSync(object.path)) {
    degradations.push(`${object.id}: image unavailable`);
    return;
  }
  slide.addImage({
    path: object.path,
    x: box.x,
    y: box.y,
    w: box.w,
    h: box.h,
    sizing: {
      type: object.crop === "contain" ? "contain" : "cover",
      w: box.w,
      h: box.h,
    },
    objectName: object.id,
    altText: String(object.alt || ""),
  });
  editableObjectCount += 1;
}

function addTableObject(slide, object, box) {
  const rows = [object.headers || [], ...(object.rows || [])];
  const rowCount = Math.max(rows.length, 1);
  const headerOptions = {
    bold: true,
    color: tokens.canvas,
    fill: tokens.ink,
  };
  const pptRows = rows.map((row, rowIndex) =>
    row.map((cell, columnIndex) => {
      const style = object.cell_styles?.[rowIndex]?.[columnIndex];
      if (!style) return {
        text: String(cell ?? ""),
        options: rowIndex === 0 ? headerOptions : {},
      };
      const fontSize = numeric(style.font_size, tokens.bodySize);
      return {
        text: richTextRuns({text: String(cell ?? ""), runs: style.runs}, fontSize, tokens.ink),
        options: {
          bold: false,
          italic: false,
          align: style.align,
          valign: style.valign === "mid" ? "middle" : style.valign,
          fontSize,
          fill: cleanHex(style.fill_color, rowIndex === 0 ? tokens.ink : tokens.canvas),
        },
      };
    }),
  );
  slide.addTable(pptRows, {
    ...box,
    objectName: object.id,
    border: { type: "solid", color: tokens.subtle, pt: 0.75 },
    fill: tokens.canvas,
    color: tokens.ink,
    fontFace: tokens.bodyFont,
    fontSize: tokens.bodySize,
    margin: 0.08,
    // Tables use the documented PptxGenJS value. Unlike textboxes, this
    // backend does not normalize "mid" and would emit an invalid OOXML anchor.
    valign: "middle",
    autoFit: false,
    rowH: Math.max(0.28, box.h / rowCount),
    breakLine: false,
  });
  editableObjectCount += 1;
}

function addChartObject(slide, object, box) {
  const series = Object.entries(object.series || {}).map(([name, values]) => ({
    name,
    labels: object.categories || [],
    values,
  }));
  if (series.length === 0) {
    degradations.push(`${object.id}: chart has no series`);
    return;
  }
  const types = {
    bar: chartType.bar,
    column: chartType.bar,
    line: chartType.line,
    pie: chartType.pie,
  };
  const isPie = object.chart_type === "pie";
  const isLine = object.chart_type === "line";
  slide.addChart(types[object.chart_type] || chartType.bar, series, {
    ...box,
    objectName: object.id,
    showTitle: false,
    showLegend: object.show_legend ?? series.length > 1,
    legendFontFace: tokens.bodyFont,
    legendFontSize: tokens.captionSize,
    chartColors: chartColorsFor({
      isPie,
      seriesCount: series.length,
      palette: tokens.chartSeries,
    }),
    barDir: object.chart_type === "column" ? "col" : "bar",
    showCatName: false,
    showPercent: isPie,
    showValue: true,
    showBorder: false,
    showGridLines: isLine,
    dataLabelPosition: isPie ? "bestFit" : "outEnd",
    dataLabelColor: tokens.ink,
    dataLabelFormatCode: object.number_format || "General",
    catAxisLabelFontFace: tokens.bodyFont,
    catAxisLabelFontSize: tokens.captionSize,
    valAxisLabelFontFace: tokens.bodyFont,
    valAxisLabelFontSize: tokens.captionSize,
    // Authored charts keep their historic zero baseline. Native imports
    // preserve both explicit scales and Office's absent (automatic) bounds.
    catAxisOrientation: object.category_axis?.orientation,
    catAxisMinVal: object.category_axis?.minimum ?? undefined,
    catAxisMaxVal: object.category_axis?.maximum ?? undefined,
    catAxisLabelPos: object.category_axis?.label_position,
    valAxisOrientation: object.value_axis?.orientation,
    valAxisMinVal: object.value_axis ? object.value_axis.minimum ?? undefined : 0,
    valAxisMaxVal: object.value_axis?.maximum ?? undefined,
    valAxisMajorUnit: object.value_axis?.major_unit ?? undefined,
    valAxisLabelPos: object.value_axis?.label_position,
    valAxisLabelFormatCode: object.value_axis?.number_format,
    showValAxis: !isPie,
  });
  editableObjectCount += 1;
}

function nativeShape(value) {
  return {
    rect: shapeType.rect,
    round_rect: shapeType.roundRect,
    ellipse: shapeType.ellipse,
    line: shapeType.line,
    chevron: shapeType.chevron,
  }[value] || shapeType.rect;
}

function addShapeObject(slide, object, box) {
  const requestedShape = nativeShape(object.shape_type);
  const nativeStyle = object.native_style;
  const line = {
    color: cleanHex(object.line_color, tokens.subtle),
    width: numeric(object.line_width, 1),
    ...(nativeStyle?.line_none && !object.line_color ? { transparency: 100 } : {}),
  };
  const fill = object.fill_color
    ? { color: cleanHex(object.fill_color, tokens.canvas) }
    : { color: tokens.canvas, transparency: 100 };
  const common = {
    ...box,
    objectName: object.id,
    rotate: numeric(object.rotate, 0),
    fill,
    line,
  };
  if (object.text && object.shape_type !== "line") {
    const size = numeric(object.font_size, tokens.bodySize);
    const color = cleanHex(object.text_color, tokens.ink);
    // A text-only shape edit keeps the immutable style snapshot. Its old
    // wording must never override the newly requested label. Replacing the
    // whole label starts with its existing first visible character style.
    const runs = Array.isArray(object.runs) ? object.runs : [];
    const firstStyle = runs.find((run) => !run.paragraph_break);
    const currentRuns = runs.map((run) => run.text).join("") === object.text
      ? runs : firstStyle ? [{ ...firstStyle, text: object.text, paragraph_break: false }] : [];
    slide.addText(richTextRuns({ ...object, runs: currentRuns }, size, color), {
      ...common,
      shape: requestedShape,
      fontFace: tokens.bodyFont,
      fontSize: size,
      color,
      align: nativeStyle?.align || "center",
      valign: nativeStyle?.valign || "mid",
      // The pinned 4.0.1 text writer serializes [left,right,bottom,top],
      // unlike its table margin order. The public native roundtrip locks it.
      margin: nativeStyle?.margin_top != null
        ? [nativeStyle.margin_left, nativeStyle.margin_right, nativeStyle.margin_bottom, nativeStyle.margin_top]
        : 0.08,
      fit: "shrink",
    });
  } else {
    slide.addShape(requestedShape, common);
  }
  editableObjectCount += 1;
}

function renderObject(slide, slideSpec, object) {
  const box = objectFrame(slideSpec, object);
  if (object.kind === "text") return addTextObject(slide, object, box);
  if (object.kind === "metric") return addMetricObject(slide, object, box);
  if (object.kind === "image") return addImageObject(slide, object, box);
  if (object.kind === "table") return addTableObject(slide, object, box);
  if (object.kind === "chart") return addChartObject(slide, object, box);
  if (object.kind === "shape") return addShapeObject(slide, object, box);
  if (object.kind === "native_group") {
    // A private placeholder only. Python validates/restores the immutable
    // native group before publishing; this marker earns no editability credit.
    slide.addShape(shapeType.rect, {
      ...box, objectName: object.id,
      fill: { color: tokens.canvas, transparency: 100 },
      line: { color: tokens.canvas, transparency: 100 },
    });
    return;
  }
  degradations.push(`${slideSpec.id}:${object.id}: unsupported kind ${object.kind}`);
}

for (const slideSpec of deck.slides || []) {
  const slide = pptx.addSlide();
  slide.background = { color: tokens.canvas };
  const objects = slideSpec.objects || [];
  objects.forEach((object) => renderObject(slide, slideSpec, object));

  // Stable identity marker for GenOffice reconciliation. It is metadata, not
  // a designed page object, and therefore is excluded from the editable count.
  slide.addShape(shapeType.rect, {
    x: 0,
    y: 0,
    w: 0.01,
    h: 0.01,
    fill: { color: tokens.canvas, transparency: 100 },
    line: { color: tokens.canvas, transparency: 100 },
    objectName: `meta:${deck.id}:${slideSpec.id}`,
  });
  if (slideSpec.speaker_notes) slide.addNotes(String(slideSpec.speaker_notes));
}

await pptx.writeFile({ fileName: outputPath });
return {
  backend: "pptxgenjs",
  editableObjectCount,
  degradations,
};
}
