// Clean-room native PowerPoint composition for Lantai Presentation Skill v3.
// Inputs are Lantai's semantic DeckIR and the public PptxGenJS API.

import fs from "node:fs";
import { createRequire } from "node:module";

const [specPath, outputPath, modulePath] = process.argv.slice(2);
if (!specPath || !outputPath || !modulePath) {
  throw new Error("usage: pptxgenjs_v3.mjs <deck.json> <output.pptx> <pptxgenjs-module>");
}
const require = createRequire(import.meta.url);
const { installImageParserMitigation } = require("./pptxgenjs_security.cjs");
installImageParserMitigation(modulePath);
const PptxGenJS = require(modulePath);
const deck = JSON.parse(fs.readFileSync(specPath, "utf8"));

const THEMES = {
  "lantai-ink": {
    paper: "F4F1EA", ink: "17212B", accent: "D9583B", soft: "E4DDD1",
    secondary: "53606B", inverse: "FFFDF8", title: "PingFang SC", body: "PingFang SC",
  },
  "lantai-slate": {
    paper: "151A20", ink: "F2EFE8", accent: "E66A4E", soft: "29313A",
    secondary: "AEB7BF", inverse: "151A20", title: "PingFang SC", body: "PingFang SC",
  },
  "lantai-warm": {
    paper: "F3EDE2", ink: "24201D", accent: "C44F34", soft: "DDD1C1",
    secondary: "746A61", inverse: "FFFCF5", title: "Songti SC", body: "PingFang SC",
  },
};
const baseTheme = THEMES[deck.theme_id] || THEMES["lantai-warm"];
const directedPalette = (deck.visual_direction?.palette || [])
  .map((value) => String(value).replace(/^#/, "").toUpperCase())
  .filter((value) => /^[0-9A-F]{6}$/.test(value));
function mixHex(first, second, secondWeight) {
  const weight = Math.max(0, Math.min(1, secondWeight));
  const channels = [0, 2, 4].map((offset) => {
    const a = Number.parseInt(first.slice(offset, offset + 2), 16);
    const b = Number.parseInt(second.slice(offset, offset + 2), 16);
    return Math.round(a * (1 - weight) + b * weight)
      .toString(16)
      .padStart(2, "0")
      .toUpperCase();
  });
  return channels.join("");
}
const directedPaper = directedPalette[0] || baseTheme.paper;
const directedInk = directedPalette[1] || baseTheme.ink;
const theme = {
  ...baseTheme,
  paper: directedPaper,
  ink: directedInk,
  accent: directedPalette[2] || baseTheme.accent,
  soft: mixHex(directedPaper, directedInk, 0.12),
  secondary: mixHex(directedInk, directedPaper, 0.42),
  inverse: directedPalette[3] || directedPaper,
};
const visualConcept = String(deck.visual_direction?.visual_concept || "").toLowerCase();
const directionStyle = /发布|首发|launch|reveal/.test(visualConcept)
  ? "launch"
  : /数据|经营|投资|冷静|data|investment|analytic/.test(visualConcept)
    ? "data"
    : /系统|蓝图|架构|system|blueprint|architecture/.test(visualConcept)
      ? "blueprint"
      : /未来|演讲|keynote|future|forum/.test(visualConcept)
        ? "keynote"
        : /人文|档案|摄影|温暖|研究|训练|公益|human|field|documentary/.test(visualConcept)
          ? "human"
          : "editorial";
const motifVariant = Math.abs(Number(deck.seed || 0)) % 3;
const pptx = new PptxGenJS();
pptx.layout = "LAYOUT_WIDE";
pptx.author = "Lantai Presentation Skill v3";
pptx.subject = deck.title;
pptx.title = deck.title;
pptx.company = "Lantai";
pptx.lang = deck.language || "zh-CN";
pptx.theme = {
  headFontFace: theme.title,
  bodyFontFace: theme.body,
  lang: deck.language || "zh-CN",
};
pptx.defineSlideMaster({
  title: "LANTAI_V3",
  background: { color: theme.paper },
  objects: [],
  slideNumber: { x: 12.25, y: 7.02, w: 0.55, h: 0.22, color: theme.secondary, fontSize: 9, align: "right" },
});

let editableCount = 0;
const degradations = [];
const shape = pptx.ShapeType;
const chartType = pptx.ChartType;

function addText(slide, obj, box, options = {}) {
  const {
    structured = true,
    leadSize,
    bodySize,
    ...textOptions
  } = options;
  const rawText = String(obj.text ?? "");
  const lines = rawText.split("\n").map((line) => line.trim()).filter(Boolean);
  const baseSize = textOptions.fontSize || obj.font_size || (obj.role === "title" ? 34 : 19);
  const value = structured && lines.length > 1 && obj.role !== "title" && obj.role !== "quote"
    ? lines.map((line, index) => ({
        text: line,
        options: {
          breakLine: index < lines.length - 1,
          bold: index === 0,
          fontSize: index === 0 ? (leadSize || Math.max(baseSize + 2, 19)) : (bodySize || Math.max(baseSize - 1, 17)),
          color: index === 0 ? (textOptions.color || theme.ink) : (textOptions.bodyColor || theme.secondary),
          paraSpaceAfterPt: index === 0 ? 8 : 3,
        },
      }))
    : rawText;
  const { bodyColor: _bodyColor, ...safeTextOptions } = textOptions;
  slide.addText(value, {
    ...box,
    objectName: obj.id,
    fontFace: safeTextOptions.fontFace || (obj.role === "title" ? theme.title : theme.body),
    fontSize: baseSize,
    color: safeTextOptions.color || theme.ink,
    bold: safeTextOptions.bold ?? Boolean(obj.emphasis || obj.role === "title" || obj.role === "heading"),
    italic: obj.role === "quote",
    margin: safeTextOptions.margin ?? 0,
    valign: safeTextOptions.valign || "mid",
    breakLine: false,
    fit: "shrink",
    ...safeTextOptions,
  });
  editableCount += 1;
}

function objectBox(obj, fallback) {
  return obj.frame ? { ...obj.frame } : fallback;
}

function addRule(slide, x, y, w, color = theme.accent, width = 1.5) {
  slide.addShape(shape.line, { x, y, w, h: 0, line: { color, width } });
}

function addGrid(slide, color = theme.soft, opacity = 68) {
  for (let x = 0.72; x < 13; x += 1.05) {
    slide.addShape(shape.line, { x, y: 0.4, w: 0, h: 6.7, line: { color, transparency: opacity, width: 0.45 } });
  }
  for (let y = 0.5; y < 7.2; y += 0.82) {
    slide.addShape(shape.line, { x: 0.55, y, w: 12.25, h: 0, line: { color, transparency: opacity, width: 0.45 } });
  }
}

function addOrbit(slide, centerX, centerY, color = theme.accent) {
  [1.0, 1.72, 2.48].forEach((radius, index) => {
    slide.addShape(shape.ellipse, {
      x: centerX - radius, y: centerY - radius, w: radius * 2, h: radius * 2,
      fill: { transparency: 100 },
      line: { color, transparency: 18 + index * 17, width: 1.1 },
    });
  });
  slide.addShape(shape.ellipse, {
    x: centerX - 0.12, y: centerY - 0.12, w: 0.24, h: 0.24,
    fill: { color }, line: { transparency: 100 },
  });
}

function addImageObject(slide, obj, target, overrides = {}) {
  if (!obj?.path || !fs.existsSync(obj.path)) {
    if (obj) degradations.push(`${obj.id}: image unavailable`);
    return false;
  }
  slide.addImage({
    path: obj.path,
    x: target.x,
    y: target.y,
    w: target.w,
    h: target.h,
    sizing: { type: obj.crop || "cover", w: target.w, h: target.h },
    objectName: obj.id,
    altText: obj.alt,
    ...overrides,
  });
  editableCount += 1;
  return true;
}

function addHeader(slide, titleObj, index) {
  if (directionStyle === "blueprint") {
    addGrid(slide, theme.soft, 82);
    slide.addShape(shape.rect, { x: 0.72, y: 0.47, w: 0.1, h: 0.77, fill: { color: theme.accent }, line: { transparency: 100 } });
    if (titleObj) addText(slide, titleObj, { x: 1.02, y: 0.46, w: 10.35, h: 0.74 }, { fontSize: 32, bold: true });
    slide.addText(`NODE / ${String(index).padStart(2, "0")}`, {
      x: 11.3, y: 0.58, w: 1.05, h: 0.28, fontFace: theme.body,
      fontSize: 9, bold: true, color: theme.accent, align: "right", charSpacing: 1.2, margin: 0,
    });
    addRule(slide, 1.02, 1.27, 11.33, theme.soft, 1);
    return;
  }
  if (directionStyle === "launch") {
    slide.addShape(shape.rect, { x: 0, y: 0, w: 13.333, h: 0.18, fill: { color: theme.accent }, line: { transparency: 100 } });
    slide.addShape(shape.roundRect, { x: 0.72, y: 0.52, w: 0.78, h: 0.38, rectRadius: 0.08, fill: { color: theme.ink }, line: { transparency: 100 } });
    slide.addText(`0${index}`, { x: 0.86, y: 0.59, w: 0.49, h: 0.17, fontFace: theme.body, fontSize: 9, bold: true, color: theme.inverse, margin: 0, align: "center" });
    if (titleObj) addText(slide, titleObj, { x: 1.76, y: 0.43, w: 9.9, h: 0.7 }, { fontSize: 32, bold: true });
    slide.addText("REVEAL", { x: 11.62, y: 0.58, w: 0.72, h: 0.2, fontFace: theme.body, fontSize: 8.5, bold: true, color: theme.accent, charSpacing: 1.4, margin: 0, align: "right" });
    return;
  }
  if (directionStyle === "data") {
    slide.addText("SIGNAL", { x: 0.72, y: 0.56, w: 0.9, h: 0.21, fontFace: theme.body, fontSize: 8.5, bold: true, color: theme.accent, charSpacing: 1.6, margin: 0 });
    if (titleObj) addText(slide, titleObj, { x: 0.72, y: 0.82, w: 10.75, h: 0.62 }, { fontSize: 32, bold: true });
    slide.addText(`${String(index).padStart(2, "0")} / ${String(deck.slides.length).padStart(2, "0")}`, { x: 11.3, y: 0.86, w: 1.05, h: 0.24, fontFace: theme.body, fontSize: 9, bold: true, color: theme.secondary, margin: 0, align: "right" });
    addRule(slide, 0.72, 1.5, 11.63, theme.ink, 1.4);
    addRule(slide, 0.72, 1.5, 2.1 + motifVariant * 0.5, theme.accent, 3.4);
    return;
  }
  if (directionStyle === "keynote") {
    slide.addShape(shape.ellipse, { x: 0.67, y: 0.52, w: 0.36, h: 0.36, fill: { color: theme.accent }, line: { transparency: 100 } });
    slide.addShape(shape.ellipse, { x: 0.79, y: 0.64, w: 0.12, h: 0.12, fill: { color: theme.paper }, line: { transparency: 100 } });
    if (titleObj) addText(slide, titleObj, { x: 1.25, y: 0.43, w: 10.2, h: 0.74 }, { fontSize: 32, bold: true });
    slide.addText(`ACT ${index}`, { x: 11.58, y: 0.57, w: 0.72, h: 0.24, fontFace: theme.body, fontSize: 9, bold: true, color: theme.accent, margin: 0, align: "right", charSpacing: 1.1 });
    return;
  }
  if (directionStyle === "human") {
    slide.addShape(shape.ellipse, { x: 0.72, y: 0.62, w: 0.28, h: 0.28, fill: { color: theme.accent }, line: { transparency: 100 } });
    if (titleObj) addText(slide, titleObj, { x: 1.22, y: 0.46, w: 9.95, h: 0.74 }, { fontSize: 32, bold: true });
    slide.addText(String(index).padStart(2, "0"), {
      x: 11.78, y: 0.55, w: 0.57, h: 0.38, fontFace: theme.title,
      fontSize: 12, color: theme.secondary, align: "right", margin: 0,
    });
    addRule(slide, 1.22, 1.27, 3.1, theme.accent, 2.1);
    addRule(slide, 4.42, 1.27, 7.93, theme.soft, 1);
    return;
  }
  if (titleObj) addText(slide, titleObj, { x: 0.72, y: 0.46, w: 10.9, h: 0.74 }, { fontSize: 32, bold: true });
  slide.addText(String(index).padStart(2, "0"), {
    x: 11.78, y: 0.55, w: 0.72, h: 0.38, fontFace: theme.body,
    fontSize: 11, bold: true, color: theme.accent, align: "right", margin: 0,
  });
  addRule(slide, 0.72, 1.27, 11.8, theme.soft, 1);
}

function addObject(slide, obj, box, options = {}) {
  const target = objectBox(obj, box);
  if (!target) {
    degradations.push(`${obj.id}: no composition frame`);
    return;
  }
  if (obj.kind === "text") return addText(slide, obj, target, options);
  if (obj.kind === "metric") {
    const valueHeight = Math.min(target.h * 0.56, 1.24);
    slide.addText(obj.value, { ...target, h: valueHeight, objectName: obj.id,
      fontFace: theme.title, fontSize: 32, bold: true, color: theme.accent, margin: 0, fit: "shrink" });
    slide.addText(`${obj.label}${obj.detail ? `\n${obj.detail}` : ""}`, {
      x: target.x, y: target.y + valueHeight, w: target.w,
      h: Math.max(0.35, target.h - valueHeight), fontFace: theme.body,
      fontSize: 17, color: theme.secondary, margin: 0, fit: "shrink",
    });
    editableCount += 1;
    return;
  }
  if (obj.kind === "image") {
    addImageObject(slide, obj, target);
    return;
  }
  if (obj.kind === "table") {
    const rows = [obj.headers, ...(obj.rows || [])];
    slide.addTable(rows, { ...target, objectName: obj.id, border: { type: "solid", color: theme.soft, pt: 1 },
      fill: theme.inverse, color: theme.ink, fontFace: theme.body, fontSize: 17,
      margin: 0.08, bold: false, autoFit: false, valign: "mid",
      rowH: Math.max(0.34, target.h / Math.max(1, rows.length)) });
    editableCount += 1;
    return;
  }
  if (obj.kind === "chart") {
    const series = Object.entries(obj.series || {}).map(([name, values]) => ({ name, labels: obj.categories, values }));
    const type = { bar: chartType.bar, column: chartType.bar, line: chartType.line, pie: chartType.pie }[obj.chart_type] || chartType.bar;
    slide.addChart(type, series, { ...target, objectName: obj.id, showLegend: series.length > 1,
      showTitle: false, catAxisLabelFontFace: theme.body,
      catAxisLabelFontSize: 17, valAxisLabelFontSize: 17, chartColors: [theme.accent, theme.ink, theme.secondary],
      barDir: obj.chart_type === "column" ? "col" : "bar",
      showCatName: false, showPercent: obj.chart_type === "pie",
      showBorder: false, showGridLines: obj.chart_type !== "pie", showValue: obj.chart_type === "pie" });
    editableCount += 1;
    return;
  }
  if (obj.kind === "shape") {
    const shapeType = {
      rect: shape.rect,
      round_rect: shape.roundRect,
      ellipse: shape.ellipse,
      line: shape.line,
      chevron: shape.chevron,
    }[obj.shape_type] || shape.rect;
    const fill = obj.shape_type === "line"
      ? { transparency: 100 }
      : { color: obj.fill_color || theme.soft };
    slide.addShape(shapeType, {
      ...target,
      objectName: obj.id,
      rotate: obj.rotate || 0,
      fill,
      line: { color: obj.line_color || theme.accent, width: obj.line_width ?? 1 },
    });
    if (obj.text) {
      slide.addText(obj.text, {
        ...target,
        fontFace: theme.body,
        fontSize: Math.max(17, obj.font_size || 17),
        color: obj.text_color || theme.ink,
        bold: true,
        align: "center",
        valign: "mid",
        margin: 0.1,
        fit: "shrink",
      });
    }
    editableCount += 1;
  }
}

function titleAndBody(objects) {
  const title = objects.find((o) => o.kind === "text" && o.slot === "title");
  const body = objects.filter((o) => o !== title);
  return { title, body };
}

function renderCover(slide, objects) {
  const title = objects.find((o) => o.kind === "text" && o.slot === "title") || objects[0];
  const image = objects.find((o) => o.kind === "image");
  const rest = objects.filter((o) => o !== title && o !== image);
  if (directionStyle === "human") {
    slide.background = { color: theme.paper };
    if (image) {
      addImageObject(slide, image, { x: 8.45, y: 0, w: 4.88, h: 7.5 });
      slide.addShape(shape.rect, { x: 8.45, y: 0, w: 4.88, h: 7.5, fill: { color: theme.ink, transparency: 68 }, line: { transparency: 100 } });
    } else {
      slide.addShape(shape.rect, { x: 8.45, y: 0, w: 4.88, h: 7.5, fill: { color: theme.ink }, line: { transparency: 100 } });
      slide.addShape(shape.arc, { x: 9.02, y: 0.92, w: 3.2, h: 3.2, adjustPoint: 0.28, rotate: 18, fill: { transparency: 100 }, line: { color: theme.accent, width: 7 } });
      slide.addShape(shape.ellipse, { x: 10.52, y: 4.92, w: 1.18, h: 1.18, fill: { color: theme.accent }, line: { transparency: 100 } });
    }
    slide.addText("FIELD NOTE / 01", { x: 0.82, y: 0.68, w: 4.2, h: 0.28, fontFace: theme.body, fontSize: 10, bold: true, color: theme.accent, charSpacing: 2.0, margin: 0 });
    if (title) addText(slide, title, { x: 0.82, y: 1.52, w: 6.92, h: 2.28 }, { fontSize: 46, color: theme.ink, bold: true, valign: "bottom", structured: false });
    rest.forEach((obj, i) => addObject(slide, obj, { x: 0.86, y: 4.52 + i * 0.64, w: 6.72, h: 0.5 }, { fontSize: 18, color: theme.secondary }));
    addRule(slide, 0.82, 6.48, 2.1, theme.accent, 3.2);
    addRule(slide, 3.08, 6.48, 4.52, theme.soft, 1.2);
    return;
  }
  if (directionStyle === "blueprint") {
    slide.background = { color: theme.ink };
    addGrid(slide, theme.soft, 83);
    slide.addShape(shape.rect, { x: 8.78, y: 0, w: 4.55, h: 7.5, fill: { color: theme.accent, transparency: 9 }, line: { transparency: 100 } });
    [0, 1, 2, 3].forEach((i) => {
      const y = 1.0 + i * 1.46;
      slide.addShape(shape.ellipse, { x: 9.28 + (i % 2) * 1.18, y, w: 0.23, h: 0.23, fill: { color: theme.inverse }, line: { transparency: 100 } });
      addRule(slide, 9.52 + (i % 2) * 1.18, y + 0.11, 2.45 - (i % 2) * 0.5, theme.inverse, 0.8);
    });
    slide.addText("SYSTEM MAP / VERSION 01", { x: 0.84, y: 0.72, w: 4.7, h: 0.28, fontFace: theme.body, fontSize: 10, bold: true, color: theme.accent, charSpacing: 1.7, margin: 0 });
    if (title) addText(slide, title, { x: 0.84, y: 1.62, w: 7.18, h: 2.05 }, { fontSize: 46, color: theme.inverse, bold: true, valign: "bottom", structured: false });
    rest.forEach((obj, i) => addObject(slide, obj, { x: 0.87, y: 4.42 + i * 0.64, w: 6.82, h: 0.5 }, { fontSize: 18, color: theme.soft }));
    slide.addText("A—01", { x: 10.7, y: 6.36, w: 1.55, h: 0.28, fontFace: theme.body, fontSize: 10, bold: true, color: theme.inverse, align: "right", margin: 0, charSpacing: 1.3 });
    return;
  }
  if (directionStyle === "launch") {
    slide.background = { color: theme.ink };
    if (image) {
      addImageObject(slide, image, { x: 8.28, y: 0, w: 5.05, h: 7.5 });
      slide.addShape(shape.rect, { x: 8.28, y: 0, w: 5.05, h: 7.5, fill: { color: theme.ink, transparency: 52 }, line: { transparency: 100 } });
    } else {
      slide.addShape(shape.ellipse, { x: 8.2, y: -0.92, w: 6.1, h: 6.1, fill: { transparency: 100 }, line: { color: theme.accent, width: 44, transparency: 4 } });
    }
    slide.addShape(shape.rect, { x: 0, y: 0, w: 0.22, h: 7.5, fill: { color: theme.accent }, line: { transparency: 100 } });
    slide.addText("PRODUCT REVEAL", { x: 0.88, y: 0.7, w: 3.2, h: 0.28, fontFace: theme.body, fontSize: 10, bold: true, color: theme.accent, charSpacing: 2.1, margin: 0 });
    if (title) addText(slide, title, { x: 0.88, y: 1.56, w: 6.72, h: 2.25 }, { fontSize: 47, color: theme.inverse, bold: true, valign: "bottom", structured: false });
    rest.forEach((obj, i) => addObject(slide, obj, { x: 0.92, y: 4.54 + i * 0.64, w: 6.46, h: 0.5 }, { fontSize: 18, color: theme.soft }));
    slide.addShape(shape.roundRect, { x: 0.88, y: 6.1, w: 1.62, h: 0.48, rectRadius: 0.08, fill: { color: theme.accent }, line: { transparency: 100 } });
    slide.addText("10 / 18", { x: 1.04, y: 6.21, w: 1.3, h: 0.18, fontFace: theme.body, fontSize: 10, bold: true, color: theme.inverse, align: "center", margin: 0, charSpacing: 1.1 });
    return;
  }
  if (directionStyle === "data") {
    slide.background = { color: theme.paper };
    slide.addShape(shape.rect, { x: 8.88, y: 0, w: 4.45, h: 7.5, fill: { color: theme.ink }, line: { transparency: 100 } });
    for (let i = 0; i < 5; i += 1) {
      const width = 1.15 + i * 0.47;
      slide.addShape(shape.rect, { x: 9.43, y: 5.86 - i * 0.88, w: width, h: 0.18, fill: { color: i === 4 ? theme.accent : theme.secondary, transparency: i === 4 ? 0 : 28 }, line: { transparency: 100 } });
    }
    slide.addText("DECISION SIGNAL", { x: 0.84, y: 0.72, w: 3.7, h: 0.28, fontFace: theme.body, fontSize: 10, bold: true, color: theme.accent, charSpacing: 2.0, margin: 0 });
    if (title) addText(slide, title, { x: 0.84, y: 1.62, w: 7.32, h: 2.1 }, { fontSize: 46, color: theme.ink, bold: true, valign: "bottom", structured: false });
    rest.forEach((obj, i) => addObject(slide, obj, { x: 0.88, y: 4.48 + i * 0.64, w: 7.1, h: 0.5 }, { fontSize: 18, color: theme.secondary }));
    addRule(slide, 0.84, 6.46, 7.32, theme.ink, 1.2);
    return;
  }
  if (directionStyle === "keynote") {
    slide.background = { color: theme.ink };
    if (image) {
      addImageObject(slide, image, { x: 8.18, y: 0, w: 5.15, h: 7.5 });
      slide.addShape(shape.rect, { x: 8.18, y: 0, w: 5.15, h: 7.5, fill: { color: theme.ink, transparency: 34 }, line: { transparency: 100 } });
    }
    addOrbit(slide, 10.64 + motifVariant * 0.2, 3.4, theme.accent);
    slide.addShape(shape.line, { x: 7.22, y: 5.72, w: 5.16, h: -4.18, line: { color: theme.accent, transparency: 42, width: 8 } });
    slide.addText("FORUM / KEYNOTE", { x: 0.86, y: 0.72, w: 3.7, h: 0.28, fontFace: theme.body, fontSize: 10, bold: true, color: theme.accent, charSpacing: 2.0, margin: 0 });
    if (title) addText(slide, title, { x: 0.86, y: 1.62, w: 7.2, h: 2.15 }, { fontSize: 47, color: theme.inverse, bold: true, valign: "bottom", structured: false });
    rest.forEach((obj, i) => addObject(slide, obj, { x: 0.9, y: 4.52 + i * 0.64, w: 6.82, h: 0.5 }, { fontSize: 18, color: theme.soft }));
    slide.addText("01", { x: 11.46, y: 6.36, w: 0.76, h: 0.44, fontFace: theme.title, fontSize: 20, bold: true, color: theme.inverse, align: "right", margin: 0 });
    return;
  }
  slide.background = { color: theme.ink };
  slide.addShape(shape.rect, { x: 0, y: 0, w: 0.2, h: 7.5, fill: { color: theme.accent }, line: { transparency: 100 } });
  slide.addText("EDITORIAL BRIEF / 01", { x: 0.86, y: 0.72, w: 4.2, h: 0.28, fontFace: theme.body, fontSize: 10, bold: true, color: theme.accent, charSpacing: 1.9, margin: 0 });
  if (title) addText(slide, title, { x: 0.86, y: 1.6, w: 9.26, h: 2.05 }, { fontSize: 47, color: theme.inverse, bold: true, valign: "bottom", structured: false });
  rest.forEach((obj, i) => addObject(slide, obj, { x: 0.89, y: 4.42 + i * 0.64, w: 8.4, h: 0.5 }, { fontSize: 18, color: theme.soft }));
  [0, 1, 2, 3].forEach((i) => {
    slide.addShape(shape.rect, { x: 10.54 + (i % 2) * 0.92, y: 1.62 + Math.floor(i / 2) * 1.54, w: 0.7, h: 1.2, fill: { color: i === motifVariant ? theme.accent : theme.soft, transparency: i === motifVariant ? 0 : 28 }, line: { transparency: 100 } });
  });
}

function renderColumns(slide, title, body, index, count) {
  addHeader(slide, title, index);
  const n = Math.max(1, Math.min(count, body.length));
  const gap = n === 2 ? 0.52 : 0.28;
  const w = (11.8 - gap * (n - 1)) / n;
  body.slice(0, n).forEach((obj, i) => {
    const x = 0.72 + i * (w + gap);
    const y = 1.72 + (n === 3 && i === 1 ? 0.34 : 0);
    const panelFill = i === 0 ? theme.ink : (i === n - 1 ? theme.soft : theme.inverse);
    const textColor = i === 0 ? theme.inverse : theme.ink;
    const bodyColor = i === 0 ? theme.soft : theme.secondary;
    slide.addShape(shape.rect, { x, y, w, h: 4.7, fill: { color: panelFill }, line: { transparency: 100 } });
    slide.addText(String(i + 1).padStart(2, "0"), { x: x + 0.28, y: y + 0.26, w: 0.62, h: 0.42, fontFace: theme.title, fontSize: 16, bold: true, color: i === 1 ? theme.accent : (i === 0 ? theme.accent : theme.secondary), margin: 0 });
    addRule(slide, x + 0.28, y + 0.92, w - 0.56, i === 1 ? theme.accent : (i === 0 ? theme.secondary : theme.ink), i === 1 ? 3 : 1.2);
    addObject(slide, obj, { x: x + 0.28, y: y + 1.2, w: w - 0.56, h: 2.95 }, { fontSize: 19, leadSize: 22, bodySize: 17, color: textColor, bodyColor, valign: "top", margin: 0.02 });
    slide.addShape(shape.rect, { x: x + 0.28, y: y + 4.28, w: 0.86 + i * 0.36, h: 0.08, fill: { color: theme.accent }, line: { transparency: 100 } });
  });
}

function renderMatrix(slide, title, body, index) {
  addHeader(slide, title, index);
  const boxes = [
    { x: 0.72, y: 1.6 }, { x: 6.76, y: 1.6 },
    { x: 0.72, y: 4.18 }, { x: 6.76, y: 4.18 },
  ];
  body.slice(0, 4).forEach((obj, i) => {
    const box = boxes[i];
    const fill = i === 0 ? theme.ink : (i === 3 ? theme.accent : (i === 1 ? theme.inverse : theme.soft));
    const foreground = i === 0 || i === 3 ? theme.inverse : theme.ink;
    const muted = i === 0 || i === 3 ? theme.soft : theme.secondary;
    slide.addShape(shape.rect, { x: box.x, y: box.y, w: 5.55, h: 2.22, fill: { color: fill }, line: { transparency: 100 } });
    slide.addText(String(i + 1).padStart(2, "0"), { x: box.x + 0.28, y: box.y + 0.24, w: 0.5, h: 0.36, fontFace: theme.title, fontSize: 13, bold: true, color: i === 3 ? theme.ink : theme.accent, margin: 0 });
    addObject(slide, obj, { x: box.x + 1.02, y: box.y + 0.28, w: 4.14, h: 1.52 }, { fontSize: 18, leadSize: 20, bodySize: 17, color: foreground, bodyColor: muted, valign: "top" });
  });
}

function renderComparison(slide, title, body, index) {
  addHeader(slide, title, index);
  const left = body[0]; const right = body[1];
  slide.addShape(shape.rect, { x: 0.72, y: 1.75, w: 5.42, h: 4.72, fill: { color: theme.soft }, line: { transparency: 100 } });
  slide.addShape(shape.rect, { x: 7.19, y: 1.75, w: 5.33, h: 4.72, fill: { color: theme.ink }, line: { transparency: 100 } });
  slide.addShape(shape.chevron, { x: 6.22, y: 3.4, w: 0.85, h: 1.25, fill: { color: theme.accent }, line: { transparency: 100 } });
  slide.addText("BEFORE", { x: 1.05, y: 2.05, w: 1.22, h: 0.28, fontFace: theme.body, fontSize: 9, bold: true, color: theme.secondary, charSpacing: 1.4, margin: 0 });
  slide.addText("AFTER", { x: 7.56, y: 2.05, w: 1.22, h: 0.28, fontFace: theme.body, fontSize: 9, bold: true, color: theme.accent, charSpacing: 1.4, margin: 0 });
  if (left) addObject(slide, left, { x: 1.05, y: 2.58, w: 4.55, h: 3.12 }, { fontSize: 20, leadSize: 23, bodySize: 17, valign: "top" });
  if (right) addObject(slide, right, { x: 7.56, y: 2.58, w: 4.42, h: 3.12 }, { fontSize: 20, leadSize: 23, bodySize: 17, color: theme.inverse, bodyColor: theme.soft, valign: "top" });
  body.slice(2).forEach((obj, i) => addObject(slide, obj, { x: 1.05 + (i % 2) * 5.9, y: 5.68, w: 4.95, h: 0.62 }, { fontSize: 17, color: i % 2 ? theme.soft : theme.secondary }));
}

function renderProcess(slide, title, body, index) {
  addHeader(slide, title, index);
  const n = Math.max(1, Math.min(5, body.length));
  const stepW = 11.5 / n;
  body.slice(0, n).forEach((obj, i) => {
    const x = 0.72 + i * stepW;
    const y = 1.82 + (i % 2) * 0.38;
    slide.addShape(shape.chevron, { x, y, w: stepW + 0.12, h: 1.18, fill: { color: i === n - 1 ? theme.accent : (i === 0 ? theme.ink : theme.soft) }, line: { transparency: 100 } });
    slide.addText(String(i + 1).padStart(2, "0"), { x: x + 0.22, y: y + 0.37, w: 0.58, h: 0.28, align: "center", fontFace: theme.title, fontSize: 13, bold: true, color: i === 0 || i === n - 1 ? theme.inverse : theme.secondary, margin: 0 });
    addObject(slide, obj, { x: x + 0.2, y: 3.72 + (i % 2) * 0.18, w: stepW - 0.42, h: 1.82 }, { fontSize: 18, leadSize: 20, bodySize: 17, valign: "top" });
    if (i < n - 1) addRule(slide, x + stepW - 0.04, 5.88, 0.44, theme.accent, 2.2);
  });
}

function renderChart(slide, title, body, index) {
  addHeader(slide, title, index);
  const chart = body.find((o) => o.kind === "chart" || o.kind === "table");
  const others = body.filter((o) => o !== chart);
  slide.addShape(shape.rect, { x: 0.72, y: 1.72, w: 8.38, h: 4.72, fill: { color: theme.inverse }, line: { color: theme.soft, width: 0.8 } });
  if (chart) addObject(slide, chart, { x: 1.02, y: 2.02, w: 7.78, h: 3.98 });
  slide.addShape(shape.rect, { x: 9.34, y: 1.72, w: 3.18, h: 4.72, fill: { color: theme.ink }, line: { transparency: 100 } });
  slide.addText("READOUT", { x: 9.7, y: 2.04, w: 1.18, h: 0.26, fontFace: theme.body, fontSize: 9, bold: true, color: theme.accent, charSpacing: 1.5, margin: 0 });
  others.slice(0, 3).forEach((obj, i) => {
    addObject(slide, obj, { x: 9.7, y: 2.5 + i * 1.34, w: 2.42, h: 1.08 }, { fontSize: 18, leadSize: 24, bodySize: 17, color: theme.inverse, bodyColor: theme.soft, valign: "top" });
    if (i < Math.min(2, others.length - 1)) addRule(slide, 9.7, 3.67 + i * 1.34, 2.42, theme.secondary, 0.8);
  });
}

function renderSection(slide, title, body, index) {
  slide.background = { color: theme.ink };
  if (directionStyle === "human") {
    slide.addShape(shape.rect, { x: 0, y: 0, w: 4.18, h: 7.5, fill: { color: theme.accent }, line: { transparency: 100 } });
    slide.addShape(shape.ellipse, { x: 1.02, y: 1.2, w: 2.12, h: 2.12, fill: { color: theme.paper }, line: { transparency: 100 } });
    slide.addShape(shape.ellipse, { x: 1.75, y: 1.93, w: 0.66, h: 0.66, fill: { color: theme.ink }, line: { transparency: 100 } });
  } else if (directionStyle === "blueprint") {
    addGrid(slide, theme.soft, 78);
  } else if (directionStyle === "keynote") {
    addOrbit(slide, 10.62, 3.8, theme.accent);
  }
  slide.addText(String(index).padStart(2, "0"), {
    x: 0.82, y: 0.72, w: 1.25, h: 0.72, fontFace: theme.title,
    fontSize: 36, bold: true, color: directionStyle === "human" ? theme.ink : theme.accent, margin: 0,
  });
  addRule(slide, 4.72, 1.07, 7.6, theme.accent, 2.2);
  if (title) addText(slide, title, { x: 4.72, y: 1.76, w: 7.15, h: 1.78 }, { fontSize: 43, color: theme.inverse, bold: true, valign: "bottom", structured: false });
  body.slice(0, 2).forEach((obj, i) => addObject(slide, obj, { x: 4.78, y: 4.18 + i * 0.72, w: 6.95, h: 0.58 }, { fontSize: 20, color: theme.soft }));
}

function renderJudgment(slide, title, body, index) {
  addHeader(slide, title, index);
  const lead = body[0];
  slide.addText("KEY JUDGMENT", { x: 0.82, y: 1.82, w: 2.0, h: 0.28, fontFace: theme.body, fontSize: 9, bold: true, color: theme.accent, charSpacing: 1.8, margin: 0 });
  if (lead) addObject(slide, lead, { x: 0.82, y: 2.3, w: 7.5, h: 3.08 }, { fontSize: 30, bold: true, valign: "mid", structured: false });
  slide.addShape(shape.rect, { x: 8.72, y: 1.72, w: 3.8, h: 4.72, fill: { color: theme.ink }, line: { transparency: 100 } });
  body.slice(1, 4).forEach((obj, i) => {
    slide.addText(String(i + 1).padStart(2, "0"), { x: 9.05, y: 2.0 + i * 1.42, w: 0.52, h: 0.28, fontFace: theme.title, fontSize: 11, bold: true, color: theme.accent, margin: 0 });
    addObject(slide, obj, { x: 9.72, y: 1.94 + i * 1.42, w: 2.37, h: 1.02 }, { fontSize: 17, color: theme.inverse, bodyColor: theme.soft, valign: "top" });
    if (i < 2) addRule(slide, 9.05, 3.16 + i * 1.42, 3.02, theme.secondary, 0.8);
  });
  addRule(slide, 0.82, 5.82, 3.18, theme.accent, 3.2);
}

function renderAgenda(slide, title, body, index) {
  addHeader(slide, title, index);
  const agenda = body[0];
  const agendaLines = String(agenda?.text || "")
    .split(/\n+/)
    .map((line) => line.trim())
    .filter(Boolean);
  const itemCount = Math.max(1, agendaLines.length);
  slide.addShape(shape.rect, { x: 0.72, y: 1.74, w: 3.05, h: 4.7, fill: { color: theme.ink }, line: { transparency: 100 } });
  slide.addText("TODAY", { x: 1.04, y: 2.08, w: 1.6, h: 0.28, fontFace: theme.body, fontSize: 9, bold: true, color: theme.accent, charSpacing: 1.7, margin: 0 });
  slide.addText(String(itemCount).padStart(2, "0"), { x: 1.02, y: 2.66, w: 1.72, h: 1.2, fontFace: theme.title, fontSize: 52, bold: true, color: theme.inverse, margin: 0 });
  slide.addText("DECISIONS", { x: 1.06, y: 4.02, w: 1.82, h: 0.28, fontFace: theme.body, fontSize: 9, bold: true, color: theme.soft, charSpacing: 1.4, margin: 0 });
  const rowGap = itemCount <= 4 ? 0.16 : 0.08;
  const rowHeight = Math.min(1.16, (4.38 - rowGap * (itemCount - 1)) / itemCount);
  const fontSize = itemCount <= 4 ? 22 : itemCount <= 6 ? 18 : 15;
  agendaLines.forEach((line, itemIndex) => {
    const y = 1.82 + itemIndex * (rowHeight + rowGap);
    const itemText = line.replace(/^0?\d+[.)、:\s-]+/, "").trim() || line;
    const active = itemIndex === 0;
    slide.addShape(shape.roundRect, {
      x: 4.02, y, w: 0.7, h: rowHeight,
      rectRadius: 0.05,
      fill: { color: active ? theme.accent : theme.soft },
      line: { transparency: 100 },
    });
    slide.addText(String(itemIndex + 1).padStart(2, "0"), {
      x: 4.1, y: y + 0.02, w: 0.54, h: rowHeight - 0.04,
      fontFace: theme.title, fontSize: Math.max(11, fontSize - 6), bold: true,
      color: active ? theme.inverse : theme.secondary, align: "center", valign: "mid", margin: 0,
    });
    addText(
      slide,
      { ...agenda, id: itemIndex === 0 ? agenda.id : `${agenda.id}__item_${itemIndex + 1}`, text: itemText },
      { x: 5.02, y, w: 6.8, h: rowHeight },
      { fontSize, color: theme.ink, valign: "mid", structured: false },
    );
    if (itemIndex < itemCount - 1) addRule(slide, 5.02, y + rowHeight + rowGap / 2, 6.8, theme.soft, 0.7);
  });
  body.slice(1, 4).forEach((obj, i) => addObject(slide, obj, { x: 8.0, y: 2.0 + i * 1.15, w: 3.8, h: 0.85 }, { fontSize: 18 }));
}

function renderHierarchy(slide, title, body, index) {
  addHeader(slide, title, index);
  const root = body[0];
  slide.addShape(shape.roundRect, { x: 0.82, y: 2.06, w: 3.78, h: 3.75, rectRadius: 0.08, fill: { color: theme.ink }, line: { transparency: 100 } });
  if (root) addObject(slide, root, { x: 1.2, y: 2.48, w: 3.02, h: 2.82 }, { fontSize: 25, bold: true, color: theme.inverse, bodyColor: theme.soft, valign: "mid" });
  slide.addShape(shape.line, { x: 4.6, y: 3.93, w: 1.0, h: 0, line: { color: theme.accent, width: 2.4, endArrowType: "triangle" } });
  const positions = [
    { x: 5.82, y: 1.72 }, { x: 9.25, y: 1.72 },
    { x: 5.82, y: 4.22 }, { x: 9.25, y: 4.22 },
  ];
  body.slice(1, 5).forEach((obj, i) => {
    const box = positions[i];
    slide.addShape(shape.rect, { x: box.x, y: box.y, w: 2.9, h: 1.92, fill: { color: i === 3 ? theme.accent : (i === 0 ? theme.soft : theme.inverse) }, line: { color: i === 3 ? theme.accent : theme.soft, width: 0.8 } });
    slide.addText(String(i + 1).padStart(2, "0"), { x: box.x + 0.22, y: box.y + 0.18, w: 0.48, h: 0.28, fontFace: theme.title, fontSize: 11, bold: true, color: i === 3 ? theme.ink : theme.accent, margin: 0 });
    addObject(slide, obj, { x: box.x + 0.82, y: box.y + 0.2, w: 1.78, h: 1.36 }, { fontSize: 17, color: i === 3 ? theme.inverse : theme.ink, bodyColor: i === 3 ? theme.inverse : theme.secondary, valign: "top" });
  });
}

function renderTable(slide, title, body, index) {
  addHeader(slide, title, index);
  const table = body.find((o) => o.kind === "table");
  const others = body.filter((o) => o !== table);
  if (table) addObject(slide, table, { x: 0.72, y: 1.5, w: 11.8, h: 4.62 });
  others.slice(0, 2).forEach((obj, i) => addObject(slide, obj, { x: 0.82 + i * 6.0, y: 6.28, w: 5.55, h: 0.55 }, { fontSize: 17, bold: i === 0 }));
}

function renderImageFocus(slide, title, body, index) {
  addHeader(slide, title, index);
  const image = body.find((o) => o.kind === "image");
  const others = body.filter((o) => o !== image);
  if (image) addObject(slide, image, { x: 0.72, y: 1.72, w: 8.38, h: 4.72 });
  else degradations.push(`${title?.id || `slide-${index}`}: image-focus requires a real image`);
  slide.addShape(shape.rect, { x: 0.72, y: 5.52, w: 8.38, h: 0.92, fill: { color: theme.ink, transparency: 12 }, line: { transparency: 100 } });
  slide.addShape(shape.rect, { x: 9.34, y: 1.72, w: 3.18, h: 4.72, fill: { color: theme.ink }, line: { transparency: 100 } });
  slide.addText("VISUAL EVIDENCE", { x: 9.7, y: 2.04, w: 2.1, h: 0.26, fontFace: theme.body, fontSize: 9, bold: true, color: theme.accent, charSpacing: 1.4, margin: 0 });
  others.slice(0, 3).forEach((obj, i) => addObject(slide, obj, { x: 9.7, y: 2.66 + i * 1.22, w: 2.4, h: 0.94 }, { fontSize: 17, color: theme.inverse, bodyColor: theme.soft, valign: "top" }));
  slide.addText(String(index).padStart(2, "0"), { x: 11.46, y: 5.8, w: 0.58, h: 0.32, fontFace: theme.title, fontSize: 13, bold: true, color: theme.accent, align: "right", margin: 0 });
}

function renderFreeform(slide, objects, index) {
  objects.forEach((obj) => addObject(slide, obj, null));
  slide.addText(String(index).padStart(2, "0"), {
    x: 11.85, y: 6.92, w: 0.55, h: 0.28, fontFace: theme.body,
    fontSize: 10, color: theme.secondary, align: "right", margin: 0,
  });
}

function renderQuote(slide, objects, index) {
  slide.background = { color: theme.ink };
  const image = objects.find((o) => o.kind === "image");
  if (image && directionStyle === "human") {
    addImageObject(slide, image, { x: 0, y: 0, w: 4.12, h: 7.5 });
    slide.addShape(shape.rect, { x: 0, y: 0, w: 4.12, h: 7.5, fill: { color: theme.ink, transparency: 62 }, line: { transparency: 100 } });
  } else if (directionStyle === "keynote") {
    addOrbit(slide, 10.52, 3.62, theme.accent);
  } else {
    slide.addShape(shape.rect, { x: 0, y: 0, w: 0.22, h: 7.5, fill: { color: theme.accent }, line: { transparency: 100 } });
  }
  slide.addText("“", { x: image && directionStyle === "human" ? 4.62 : 0.8, y: 0.52, w: 1.0, h: 1.2, fontFace: theme.title, fontSize: 72, color: theme.accent, margin: 0 });
  const quote = objects.find((o) => o.kind === "text" && o.role === "quote") || objects[0];
  const quoteX = image && directionStyle === "human" ? 5.1 : 1.28;
  const quoteW = image && directionStyle === "human" ? 6.85 : 10.7;
  if (quote) addText(slide, quote, { x: quoteX, y: 1.35, w: quoteW, h: 3.6 }, { fontSize: 31, color: theme.inverse, bold: false, structured: false });
  const attribution = objects.find((o) => o !== quote && o.kind !== "image");
  if (attribution) addObject(slide, attribution, { x: image && directionStyle === "human" ? 7.45 : 7.6, y: 5.55, w: image && directionStyle === "human" ? 4.5 : 4.25, h: 0.52 }, { fontSize: 17, color: theme.soft, align: "right" });
  slide.addText(String(index).padStart(2, "0"), { x: 11.8, y: 6.88, w: 0.55, h: 0.28, fontFace: theme.body, fontSize: 10, color: theme.accent, align: "right", margin: 0 });
}

function renderClosing(slide, objects) {
  const title = objects.find((o) => o.kind === "text" && o.slot === "title") || objects[0];
  const rest = objects.filter((o) => o !== title);
  if (directionStyle === "human") {
    slide.background = { color: theme.paper };
    slide.addShape(shape.rect, { x: 0, y: 0, w: 4.25, h: 7.5, fill: { color: theme.ink }, line: { transparency: 100 } });
    slide.addShape(shape.ellipse, { x: 2.72, y: 4.56, w: 1.0, h: 1.0, fill: { color: theme.accent }, line: { transparency: 100 } });
    if (title) addText(slide, title, { x: 4.94, y: 1.34, w: 7.15, h: 1.65 }, { fontSize: 44, color: theme.ink, bold: true });
    rest.forEach((obj, i) => addObject(slide, obj, { x: 4.98, y: 3.72 + i * 0.72, w: 6.78, h: 0.55 }, { fontSize: 18, color: theme.secondary }));
    addRule(slide, 4.94, 5.62, 3.35, theme.accent, 2.2);
    return;
  }
  if (directionStyle === "blueprint") {
    slide.background = { color: theme.ink };
    addGrid(slide, theme.soft, 82);
    slide.addShape(shape.rect, { x: 0, y: 0, w: 0.26, h: 7.5, fill: { color: theme.accent }, line: { transparency: 100 } });
    slide.addShape(shape.rect, { x: 9.62, y: 0, w: 3.71, h: 7.5, fill: { color: theme.soft }, line: { transparency: 100 } });
    if (title) addText(slide, title, { x: 0.9, y: 1.35, w: 7.85, h: 1.65 }, { fontSize: 44, color: theme.inverse, bold: true });
    rest.forEach((obj, i) => addObject(slide, obj, { x: 0.94, y: 3.62 + i * 0.72, w: 7.6, h: 0.55 }, { fontSize: 18, color: theme.soft }));
    slide.addText("END", { x: 10.14, y: 5.62, w: 2.18, h: 0.62, fontFace: theme.title, fontSize: 30, bold: true, color: theme.ink, align: "right", margin: 0 });
    return;
  }
  if (directionStyle === "launch") {
    slide.background = { color: theme.ink };
    slide.addShape(shape.rect, { x: 9.08, y: 0, w: 4.25, h: 7.5, fill: { color: theme.accent }, line: { transparency: 100 } });
    slide.addShape(shape.chevron, { x: 10.32, y: 2.7, w: 1.8, h: 1.8, fill: { color: theme.inverse }, line: { transparency: 100 } });
    if (title) addText(slide, title, { x: 0.9, y: 1.32, w: 7.35, h: 1.72 }, { fontSize: 44, color: theme.inverse, bold: true });
    rest.forEach((obj, i) => addObject(slide, obj, { x: 0.94, y: 3.66 + i * 0.72, w: 7.2, h: 0.54 }, { fontSize: 18, color: theme.soft }));
    return;
  }
  if (directionStyle === "data") {
    slide.background = { color: theme.paper };
    slide.addShape(shape.rect, { x: 0, y: 0, w: 3.7, h: 7.5, fill: { color: theme.ink }, line: { transparency: 100 } });
    [0, 1, 2, 3].forEach((i) => slide.addShape(shape.rect, { x: 0.78, y: 5.48 - i * 0.78, w: 0.62 + i * 0.46, h: 0.16, fill: { color: i === 3 ? theme.accent : theme.secondary }, line: { transparency: 100 } }));
    if (title) addText(slide, title, { x: 4.48, y: 1.34, w: 7.42, h: 1.76 }, { fontSize: 43, color: theme.ink, bold: true });
    rest.forEach((obj, i) => addObject(slide, obj, { x: 4.52, y: 3.72 + i * 0.72, w: 7.1, h: 0.54 }, { fontSize: 18, color: theme.secondary }));
    addRule(slide, 4.48, 5.62, 4.25, theme.accent, 3);
    return;
  }
  if (directionStyle === "keynote") {
    slide.background = { color: theme.ink };
    addOrbit(slide, 10.8, 3.7, theme.accent);
    slide.addShape(shape.line, { x: 8.1, y: 6.1, w: 4.4, h: -4.2, line: { color: theme.accent, width: 7, transparency: 45 } });
    if (title) addText(slide, title, { x: 0.9, y: 1.32, w: 7.28, h: 1.86 }, { fontSize: 44, color: theme.inverse, bold: true });
    rest.forEach((obj, i) => addObject(slide, obj, { x: 0.94, y: 3.72 + i * 0.72, w: 7.04, h: 0.54 }, { fontSize: 18, color: theme.soft }));
    return;
  }
  slide.background = { color: theme.accent };
  slide.addShape(shape.rect, { x: 0, y: 0, w: 4.15, h: 7.5, fill: { color: theme.ink }, line: { transparency: 100 } });
  if (title) addText(slide, title, { x: 4.82, y: 1.35, w: 7.05, h: 1.72 }, { fontSize: 44, color: theme.inverse, bold: true });
  rest.forEach((obj, i) => addObject(slide, obj, { x: 4.86, y: 3.62 + i * 0.72, w: 6.72, h: 0.5 }, { fontSize: 18, color: theme.inverse }));
  slide.addShape(shape.line, { x: 1.1, y: 5.55, w: 1.8, h: 0, line: { color: theme.inverse, width: 4, beginArrowType: "none", endArrowType: "triangle" } });
}

deck.slides.forEach((slideSpec, idx) => {
  const slide = pptx.addSlide("LANTAI_V3");
  const objects = slideSpec.objects || [];
  const { title, body } = titleAndBody(objects);
  const layout = slideSpec.layout_id;
  if (layout === "cover") renderCover(slide, objects);
  else if (layout === "closing") renderClosing(slide, objects);
  else if (layout === "quote") renderQuote(slide, objects, idx + 1);
  else if (layout === "freeform") renderFreeform(slide, objects, idx + 1);
  else if (layout === "section") renderSection(slide, title, body, idx + 1);
  else if (layout === "key_judgment") renderJudgment(slide, title, body, idx + 1);
  else if (layout === "agenda") renderAgenda(slide, title, body, idx + 1);
  else if (layout === "comparison") renderComparison(slide, title, body, idx + 1);
  else if (layout === "two_column") renderColumns(slide, title, body, idx + 1, 2);
  else if (layout === "process") renderProcess(slide, title, body, idx + 1);
  else if (layout === "hierarchy") renderHierarchy(slide, title, body, idx + 1);
  else if (layout === "matrix") renderMatrix(slide, title, body, idx + 1);
  else if (layout === "table") renderTable(slide, title, body, idx + 1);
  else if (layout === "image_focus") renderImageFocus(slide, title, body, idx + 1);
  else if (layout === "chart" || layout === "data_conclusion") renderChart(slide, title, body, idx + 1);
  else if (layout === "three_cards") renderColumns(slide, title, body, idx + 1, 3);
  else {
    degradations.push(`${slideSpec.id}: unsupported layout ${layout}`);
  }
  if (slideSpec.speaker_notes) slide.addNotes([slideSpec.speaker_notes]);
});

await pptx.writeFile({ fileName: outputPath });
process.stdout.write(JSON.stringify({
  engine: "pptxgenjs_v3",
  editable_object_count: editableCount,
  degradations,
}));
