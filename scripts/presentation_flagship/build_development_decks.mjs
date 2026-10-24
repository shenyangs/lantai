import fs from "node:fs/promises";
import path from "node:path";
import { execFile } from "node:child_process";
import { pathToFileURL } from "node:url";
import { promisify } from "node:util";

const workspaceDir = process.cwd();
const skillDir = process.env.SKILL_DIR;
const runtimePython = process.env.RUNTIME_PYTHON;
const runtimeNodeModules = process.env.RUNTIME_NODE_MODULES;
const revision = process.env.DECK_REVISION ?? "r1";

if (![skillDir, runtimePython].every((value) => path.isAbsolute(value ?? "")) ||
    (runtimeNodeModules && !path.isAbsolute(runtimeNodeModules))) {
  throw new Error("SKILL_DIR and RUNTIME_PYTHON must be absolute paths; RUNTIME_NODE_MODULES, when set, must be absolute");
}

const artifactToolModule = runtimeNodeModules
  ? pathToFileURL(path.join(runtimeNodeModules, "@oai/artifact-tool/dist/artifact_tool.mjs")).href
  : "@oai/artifact-tool";
const { Presentation, PresentationFile } = await import(artifactToolModule);
const { applyPresentationChartFont, finalizePresentation } = await import(
  pathToFileURL(path.join(skillDir, "container_tools/artifact_tool_utils.mjs")).href
);
const execFileAsync = promisify(execFile);

const FONT = "PingFang SC";
const W = 1280;
const H = 720;
const EXPECTED_SLIDE_SIZE_EMU = "12192000,6858000";
const OUTPUT_DIR = path.join(workspaceDir, "artifacts/presentation-flagship/20260922");
const STAGING_ROOT = path.join(workspaceDir, ".codex-finalizer/presentation-flagship", revision);

function nowMs() {
  return Number(process.hrtime.bigint()) / 1_000_000;
}

function presentation() {
  return Presentation.create({ slideSize: { width: W, height: H } });
}

function addText(slide, text, x, y, width, height, options = {}) {
  const shape = slide.shapes.add({
    geometry: "textbox",
    position: { left: x, top: y, width, height },
    fill: options.fill ?? "none",
    line: options.line ?? { fill: "none", width: 0 },
    borderRadius: options.borderRadius,
  });
  shape.text = text;
  shape.text.style = {
    typeface: FONT,
    fontSize: options.fontSize ?? 24,
    bold: options.bold ?? false,
    color: options.color ?? "#18212B",
    alignment: options.alignment ?? "left",
    autoFit: options.autoFit ?? "shrinkText",
  };
  return shape;
}

function addRect(slide, x, y, width, height, options = {}) {
  return slide.shapes.add({
    geometry: options.geometry ?? "rect",
    position: { left: x, top: y, width, height },
    fill: options.fill ?? "none",
    line: options.line ?? { fill: "none", width: 0 },
    borderRadius: options.borderRadius,
  });
}

function addRule(slide, x, y, width, color, weight = 2) {
  return slide.shapes.add({
    geometry: "line",
    position: { left: x, top: y, width, height: 0 },
    fill: "none",
    line: { style: "solid", fill: color, width: weight },
  });
}

function addCircle(slide, x, y, size, fill, line = { fill: "none", width: 0 }) {
  return slide.shapes.add({
    geometry: "ellipse",
    position: { left: x, top: y, width: size, height: size },
    fill,
    line,
  });
}

function notes(slide, text) {
  slide.speakerNotes.textFrame.setText(text);
}

function footer(slide, number, label, color = "#69737D") {
  addText(slide, label, 64, 677, 720, 22, { fontSize: 12, color });
  addText(slide, String(number).padStart(2, "0"), 1170, 674, 48, 24, {
    fontSize: 13,
    bold: true,
    color,
    alignment: "right",
  });
}

function sectionTitle(slide, number, title, subtitle, palette) {
  addText(slide, String(number).padStart(2, "0"), 64, 48, 64, 40, {
    fontSize: 15,
    bold: true,
    color: palette.accent,
  });
  addText(slide, title, 64, 83, 1120, 58, {
    fontSize: 38,
    bold: true,
    color: palette.ink,
  });
  if (subtitle) {
    addText(slide, subtitle, 66, 142, 1080, 34, {
      fontSize: 17,
      color: palette.muted,
    });
  }
}

function styleTable(table, rows, columns, palette, options = {}) {
  table.borders.assign({ style: "solid", fill: options.border ?? palette.rule, width: 1 });
  table.cells.block({ row: 0, column: 0, rowCount: rows, columnCount: columns }).assign({
    fill: options.bodyFill ?? palette.paper,
    textStyle: { typeface: FONT, fontSize: options.fontSize ?? 16, color: palette.ink },
    borders: { style: "solid", fill: options.border ?? palette.rule, width: 1 },
    anchor: "middle",
  });
  table.cells.block({ row: 0, column: 0, rowCount: 1, columnCount: columns }).assign({
    fill: options.headerFill ?? palette.ink,
    textStyle: { typeface: FONT, fontSize: options.headerFontSize ?? 15, color: options.headerColor ?? "#FFFFFF", bold: true },
    borders: { style: "solid", fill: options.headerFill ?? palette.ink, width: 1 },
    anchor: "middle",
  });
}

function addChart(slide, type, config) {
  const chart = slide.charts.add(type, config);
  applyPresentationChartFont(chart, { fontFamily: FONT });
  return chart;
}

function editorialPage(presentation, palette, number, title, subtitle, left, right, sourceNote) {
  const s = presentation.slides.add();
  s.background.fill = number % 2 ? palette.paper : palette.bg;
  sectionTitle(s, number, title, subtitle, palette);
  addText(s, left.label, 72, 228, 490, 31, { fontSize: 15, bold: true, color: palette.accent });
  addText(s, left.headline, 72, 276, 490, 104, { fontSize: 32, bold: true, color: palette.ink });
  addText(s, left.body, 74, 402, 494, 174, { fontSize: 20, color: palette.muted });
  addRect(s, 626, 230, 2, 350, { fill: palette.rule });
  addText(s, right.label, 688, 228, 480, 31, { fontSize: 15, bold: true, color: palette.accent });
  addText(s, right.headline, 688, 276, 480, 104, { fontSize: 32, bold: true, color: palette.ink });
  addText(s, right.body, 690, 402, 482, 174, { fontSize: 20, color: palette.muted });
  footer(s, number, sourceNote, palette.muted);
  notes(s, sourceNote);
  return s;
}

function buildD01() {
  const p = presentation();
  const C = {
    bg: "#F2EFE8", paper: "#FBFAF6", ink: "#172026", muted: "#5E686E",
    accent: "#C84232", accentSoft: "#F2D9D3", gold: "#C6923E", rule: "#D7D0C6",
    good: "#3E7864", warn: "#B56A2B",
  };

  {
    const s = p.slides.add();
    s.background.fill = C.bg;
    addRect(s, 0, 0, 18, H, { fill: C.accent });
    addText(s, "PROJECT M · 工作坊复盘", 76, 60, 600, 34, { fontSize: 15, bold: true, color: C.accent });
    addText(s, "先把结论、成品\n与边界交给人", 76, 150, 760, 174, { fontSize: 54, bold: true, color: C.ink, autoFit: "none" });
    addText(s, "一次关于 AI 任务协作体验的取舍与四周落地计划", 80, 358, 720, 38, { fontSize: 22, color: C.muted });
    addRule(s, 80, 458, 1010, C.rule, 2);
    addText(s, "选择：方案 B", 80, 492, 260, 44, { fontSize: 25, bold: true, color: C.accent });
    addText(s, "先交付主问题、证据与最终结论；过程与角色退到第二层，但 Stop 与状态仍清楚可见。", 348, 488, 760, 76, { fontSize: 21, color: C.ink });
    addText(s, "合成开发材料 · 非真实公司结论", 80, 628, 430, 30, { fontSize: 14, color: C.muted });
    footer(s, 1, "PPT 标杆能力开发集 D01", C.muted);
    notes(s, "Source: D01_workshop_notes.md（合成开发材料）。必须保留的用户原话：‘别让我先读一遍系统说明才知道你准备解决什么’。此页仅表达方案取舍，不代表验收已经达成。");
  }

  {
    const s = p.slides.add();
    s.background.fill = C.paper;
    sectionTitle(s, 2, "把“系统做了什么”改写成“人现在能做什么”", "方案 B 的价值不在隐藏过程，而在重新排序信息。", C);
    addText(s, "用户先看到", 70, 224, 240, 32, { fontSize: 15, bold: true, color: C.accent });
    addText(s, "结论", 70, 260, 280, 66, { fontSize: 42, bold: true, color: C.ink });
    addText(s, "可用文件", 70, 342, 280, 66, { fontSize: 42, bold: true, color: C.ink });
    addText(s, "边界", 70, 424, 280, 66, { fontSize: 42, bold: true, color: C.ink });
    addRule(s, 370, 232, 2, C.rule, 2);
    addText(s, "证据仍在，但默认收束", 420, 224, 500, 36, { fontSize: 25, bold: true, color: C.ink });
    addText(s, "需要时展开来源表、检查记录与失败上下文；不让重复台账标签淹没主判断。", 420, 274, 690, 74, { fontSize: 21, color: C.muted });
    addText(s, "过程仍可控", 420, 392, 500, 36, { fontSize: 25, bold: true, color: C.ink });
    addText(s, "Pause 可看草稿，Stop 阻止发布，重试承接原任务和版本，而不是只承诺“已重新生成”。", 420, 442, 690, 82, { fontSize: 21, color: C.muted });
    addRect(s, 420, 568, 640, 4, { fill: C.accent });
    footer(s, 2, "决策 / 信息优先级", C.muted);
    notes(s, "Source: D01_workshop_notes.md。原始材料要求保留证据可展开、过程可见与 Stop 语义；方案 B 不等于隐藏失败或权限状态。");
  }

  {
    const s = p.slides.add();
    s.background.fill = C.paper;
    sectionTitle(s, 3, "48 个任务说明了“哪里断”，还不能说明“整体成功率”", "4 周、12 名测试者；四个结果类别互斥，总数为 48。", C);
    addChart(s, "bar", {
      position: { left: 70, top: 210, width: 770, height: 380 },
      title: "任务结果（个）",
      titlePlacement: "aboveChart",
      // Keynote lays out categories farther left than the local renderer;
      // preserve the source's narrower claim and verify the rendered fit.
      categories: ["文件可打开、无需接管", "需手工排版", "缺输入暂停", "超时无文件"],
      series: [{
        name: "任务数",
        values: [31, 9, 5, 3],
        fill: C.accent,
        points: [
          { idx: 0, fill: C.good }, { idx: 1, fill: C.gold },
          { idx: 2, fill: C.warn }, { idx: 3, fill: C.accent },
        ],
      }],
      barOptions: { direction: "bar", grouping: "clustered", gapWidth: 52 },
      hasLegend: false,
      xAxis: { visible: false, min: 0, max: 36, majorGridlines: null },
      yAxis: { visible: true, line: { style: "solid", fill: C.rule, width: 1 }, textStyle: { fill: C.ink, fontSize: 14 } },
      dataLabels: { showValue: true, position: "outEnd", textStyle: { fill: C.ink, fontSize: 15, bold: true } },
      chartFill: C.paper,
      plotAreaFill: C.paper,
      chartLine: { fill: "none", width: 0 },
      plotAreaLine: { fill: "none", width: 0 },
    });
    addText(s, "31", 915, 234, 180, 72, { fontSize: 56, bold: true, color: C.good });
    addText(s, "文件可打开，无需接管", 918, 300, 300, 32, { fontSize: 18, bold: true, color: C.ink });
    addText(s, "17", 915, 386, 180, 72, { fontSize: 56, bold: true, color: C.accent });
    addText(s, "仍需接管或未完成", 918, 452, 230, 32, { fontSize: 18, bold: true, color: C.ink });
    addText(s, "样本来自定向测试，不能外推为一般成功率。", 918, 526, 250, 66, { fontSize: 17, color: C.muted });
    footer(s, 3, "证据 / 任务结果", C.muted);
    notes(s, "Source: D01_workshop_notes.md。图内短标签对应：31 可用文件、无需接管；9 有草稿、需手工排版；5 缺输入而暂停；3 超时且无最终文件。31+9+5+3=48。12 名测试者、4 周。材料明确要求不得把这组定向测试写成一般成功率。");
  }

  {
    const s = p.slides.add();
    s.background.fill = C.bg;
    sectionTitle(s, 4, "8 个追踪任务发现两类需核查的问题", "第二轮发现 2 处单位错误、1 个无来源数字；样本太小，不能外推。", C);
    const table = s.tables.add({
      rows: 3, columns: 3, left: 70, top: 226, width: 1140, height: 252,
      columnWidths: [245, 160, 735],
      values: [
        ["复核发现", "数量", "后续核查动作（建议）"],
        ["单位错误", "2 处", "核对原文单位与展示标签；不外推整体可信度"],
        ["无来源数字", "1 处", "先补来源再判断；缺来源不等于数值已证实错误"],
      ],
    });
    styleTable(table, 3, 3, C, { headerFill: C.ink, bodyFill: C.paper, fontSize: 20, headerFontSize: 20 });
    addText(s, "设计裁决", 70, 530, 160, 30, { fontSize: 15, bold: true, color: C.accent });
    addText(s, "来源与裁决共用一份事实底稿，但每个小组保留自己的判断；不能用重复标签替代冲突解释。", 70, 568, 1020, 66, { fontSize: 22, bold: true, color: C.ink });
    footer(s, 4, "证据 / 交叉复核", C.muted);
    notes(s, "Source: D01_workshop_notes.md（SRC-M01）。8 个手工追踪任务中，第二轮找到 2 处单位错误和 1 个无明确来源的数字；另有 3 个任务第二轮只增加赞同，没有新的证据。表格右栏是建议动作，不是已完成事实；不计算或声称统计显著性。3 个空泛赞同任务另见第 11 页。");
  }

  {
    const s = p.slides.add();
    s.background.fill = C.paper;
    sectionTitle(s, 5, "版本不是文件名：三种状态必须同时存在", "发生过一次：AI 从 v1 重做，覆盖了人工补充风险的 v2。", C);
    const lanes = [
      { x: 76, n: "01", title: "当前作品", body: "用户正在阅读、编辑和准备交付的版本。", fill: C.ink, fg: "#FFFFFF" },
      { x: 430, n: "02", title: "历史版本", body: "可回看、可比较，但不会被默认覆盖。", fill: C.bg, fg: C.ink },
      { x: 784, n: "03", title: "AI 候选", body: "局部修改的建议版本；合并前不替换当前作品。", fill: C.accentSoft, fg: C.ink },
    ];
    for (const lane of lanes) {
      addText(s, lane.n, lane.x, 232, 60, 34, { fontSize: 14, bold: true, color: lane.fg, fill: lane.fill });
      addText(s, lane.title, lane.x, 274, 300, 50, { fontSize: 28, bold: true, color: lane.fg, fill: lane.fill });
      addText(s, lane.body, lane.x, 334, 300, 132, { fontSize: 19, color: lane.fg, fill: lane.fill });
      addRect(s, lane.x, 470, 300, 4, { fill: lane.x === 784 ? C.accent : C.rule });
    }
    addText(s, "同名文件 ≠ 同一版本", 76, 548, 360, 42, { fontSize: 24, bold: true, color: C.accent });
    addText(s, "PDF 只是另一种格式，也不能被称为内容 v3。", 438, 548, 650, 42, { fontSize: 22, color: C.ink });
    footer(s, 5, "体验规则 / 版本连续性", C.muted);
    notes(s, "Source: D01_workshop_notes.md。材料只确认存在覆盖风险，不声称已经造成不可恢复的数据丢失。");
  }

  {
    const s = p.slides.add();
    s.background.fill = C.bg;
    sectionTitle(s, 6, "Pause、Stop、断线与重试，必须产生不同后果", "任务状态的名字只有在发布边界不同的时候才有意义。", C);
    const flows = [
      { y: 226, label: "Pause", desc: "保留阶段与草稿，可查看、可继续", color: C.gold, outcome: "不发布" },
      { y: 326, label: "Stop", desc: "标记用户停止；后续线程即使完成也不得晋升", color: C.accent, outcome: "阻止发布" },
      { y: 426, label: "断线 / 超时", desc: "先查状态；外部结果不确定时只读探测", color: C.muted, outcome: "查询优先" },
      { y: 526, label: "Retry", desc: "承接原任务、版本与幂等键，不以一句承诺收尾", color: C.good, outcome: "继续原任务" },
    ];
    for (const f of flows) {
      addCircle(s, 78, f.y + 8, 22, f.color);
      addText(s, f.label, 124, f.y, 230, 42, { fontSize: 24, bold: true, color: C.ink });
      addText(s, f.desc, 350, f.y, 610, 48, { fontSize: 19, color: C.ink });
      addText(s, f.outcome, 1010, f.y, 160, 42, { fontSize: 18, bold: true, color: f.color, alignment: "right" });
      if (f.y < 526) addRule(s, 124, f.y + 70, 1048, C.rule, 1);
    }
    footer(s, 6, "控制 / 恢复语义", C.muted);
    notes(s, "Source: D01_workshop_notes.md。材料提到用户曾把子任务完成误解为整体完成，也观察到两次陈旧进度，但未确认发生重复执行；本页不把陈旧进度直接写成重复执行根因。");
  }

  {
    const s = p.slides.add();
    s.background.fill = C.paper;
    sectionTitle(s, 7, "理想路径只保留五个可感知的交接点", "过程可追溯，但用户不必先学习内部术语。", C);
    const steps = [
      ["01", "问题", "先确认要解决的决策"],
      ["02", "草稿", "尽早给可看的真实文件"],
      ["03", "证据", "主张可展开到来源"],
      ["04", "修正", "改目标页，不覆盖手工版"],
      ["05", "交付", "文件、边界与下一步齐全"],
    ];
    steps.forEach((step, i) => {
      const x = 72 + i * 232;
      addCircle(s, x, 252, 50, i === 4 ? C.accent : C.ink);
      addText(s, step[0], x + 3, 264, 44, 24, { fontSize: 12, bold: true, color: "#FFFFFF", alignment: "center" });
      if (i < 4) addRule(s, x + 56, 277, 166, C.rule, 2);
      addText(s, step[1], x, 330, 190, 42, { fontSize: 27, bold: true, color: C.ink });
      addText(s, step[2], x, 388, 190, 100, { fontSize: 18, color: C.muted });
    });
    addRect(s, 72, 540, 1132, 56, { fill: C.bg });
    addText(s, "任何一步失败，都保留已有草稿与可观察阶段；不会把“已开始重试”写成“已交付”。", 96, 552, 1080, 34, { fontSize: 19, bold: true, color: C.ink });
    footer(s, 7, "体验模型 / 五段交接", C.muted);
    notes(s, "Source: D01_workshop_notes.md。此流程是基于材料的设计提炼，不是当前产品已经全部满足的事实陈述。");
  }

  {
    const s = p.slides.add();
    s.background.fill = C.bg;
    sectionTitle(s, 8, "四周只做一条链，但每周都保留一次失败全流程", "团队：3 名工程师 + 1 名设计师。", C);
    const weeks = [
      ["W1", "重复交付与版本覆盖", "失败流：同输入追踪全任务"],
      ["W2", "关掉版本覆盖风险", "失败流：超时后先查结果"],
      ["W3", "结论层级与证据表", "失败流：冲突不被摊平"],
      ["W4", "扩展角色展示", "失败流：仍跑完整未通过案例"],
    ];
    weeks.forEach((week, i) => {
      const x = 70 + i * 286;
      addText(s, week[0], x, 230, 80, 38, { fontSize: 17, bold: true, color: C.accent });
      addText(s, week[1], x, 282, 246, 90, { fontSize: 24, bold: true, color: C.ink });
      addRule(s, x, 392, 226, i === 3 ? C.accent : C.rule, 4);
      addText(s, week[2], x, 420, 236, 88, { fontSize: 17, color: C.muted });
    });
    addText(s, "为什么每周都跑失败流", 70, 560, 290, 34, { fontSize: 18, bold: true, color: C.ink });
    addText(s, "成功路径不能证明恢复、版本和权限边界正确。", 370, 558, 660, 38, { fontSize: 22, color: C.accent });
    footer(s, 8, "执行 / 四周计划", C.muted);
    notes(s, "Source: D01_workshop_notes.md。团队配置与周次目标来自材料；未提供成本数据，不做 ROI 估算。");
  }

  {
    const s = p.slides.add();
    s.background.fill = C.paper;
    sectionTitle(s, 9, "同一份来源，采购、财务、安全各下自己的判断", "供应商比较不需要四段相同背景；材料没有具体供应商与数值。", C);
    addText(s, "共享事实底稿", 74, 226, 276, 34, { fontSize: 16, bold: true, color: C.accent });
    addText(s, "交付时间  /  年成本  /  数据权限", 74, 270, 1010, 58, { fontSize: 33, bold: true, color: C.ink });
    addRule(s, 74, 350, 1096, C.rule, 2);
    const lenses = [
      ["采购", "交期", "什么时候能交？"],
      ["财务", "年成本", "一年要花多少？"],
      ["安全", "权限边界", "哪些数据可交给谁？"],
    ];
    lenses.forEach(([owner, subject, question], i) => {
      const x = 74 + i * 372;
      addText(s, owner, x, 384, 100, 30, { fontSize: 16, bold: true, color: C.accent });
      addText(s, subject, x, 430, 316, 53, { fontSize: 30, bold: true, color: C.ink });
      addText(s, question, x, 502, 316, 56, { fontSize: 20, color: C.muted });
      if (i < 2) addRect(s, x + 336, 388, 2, 166, { fill: C.rule });
    });
    addText(s, "出现分歧时，记录责任人和待查证据；不复制背景充当裁决。", 74, 594, 1080, 37, { fontSize: 20, bold: true, color: C.ink });
    footer(s, 9, "协作 / 一份事实、三种判断", C.muted);
    notes(s, "D01 · SRC-M01：小组 B 的供应商比较为场景记录；采购、财务和安全关注交期、年成本、数据权限。材料没有具体供应商与数值，故不补造。");
  }

  {
    const s = p.slides.add();
    s.background.fill = C.paper;
    sectionTitle(s, 10, "协作贡献按“新增了什么证据”展示", "四位成员都返回内容，不等于四份内容都有增量。", C);
    const labels = ["来源", "判断", "异议", "裁决"];
    const bodies = ["原始记录与口径", "各专业的不同关注", "冲突在哪里、尚缺什么", "为何采纳或暂不采纳"];
    labels.forEach((label, i) => {
      const y = 218 + i * 94;
      addText(s, String(i + 1).padStart(2, "0"), 76, y, 64, 30, { fontSize: 17, bold: true, color: C.accent });
      addText(s, label, 160, y - 4, 220, 44, { fontSize: 28, bold: true, color: C.ink });
      addText(s, bodies[i], 430, y, 670, 40, { fontSize: 21, color: C.muted });
      if (i < 3) addRule(s, 160, y + 62, 950, C.rule, 1);
    });
    addText(s, "只说“同意”没有新增证据，就不伪装成一次独立复核。", 76, 608, 1000, 37, { fontSize: 21, bold: true, color: C.accent });
    footer(s, 10, "协作 / 贡献可见性", C.muted);
    notes(s, "D01 · SRC-M01：小组 B 观察四位成员内容重复；小组 C 观察 3 个任务第二轮只增加赞同。此页为呈现规则，不声称已部署。");
  }

  {
    const s = p.slides.add();
    s.background.fill = C.paper;
    sectionTitle(s, 11, "第二轮只说“同意”，不能算交叉审阅", "8 个手工追踪任务只揭示修复方向，不能外推整体准确率。", C);
    addText(s, "3", 72, 220, 190, 164, { fontSize: 116, bold: true, color: C.accent });
    addText(s, "第二轮只增加赞同", 270, 258, 780, 58, { fontSize: 39, bold: true, color: C.ink });
    addText(s, "没有新证据，就不把“同意”记成独立复核。", 274, 330, 850, 46, { fontSize: 22, color: C.muted });
    addRule(s, 76, 422, 1088, C.rule, 2);
    addText(s, "2 处单位错误", 76, 462, 470, 55, { fontSize: 31, bold: true, color: C.ink });
    addText(s, "1 个无来源数字", 660, 462, 490, 55, { fontSize: 31, bold: true, color: C.ink });
    addText(s, "回到原文核口径", 76, 530, 470, 36, { fontSize: 19, color: C.muted });
    addText(s, "回到原文找依据", 660, 530, 490, 36, { fontSize: 19, color: C.muted });
    addText(s, "保留分歧、证据和裁决理由；缺来源不自动等于数值错误。", 76, 604, 1080, 36, { fontSize: 20, bold: true, color: C.accent });
    footer(s, 11, "复核 / 证据增量", C.muted);
    notes(s, "D01 · SRC-M01：小组 C 的 8 个追踪任务、2 处单位错误、1 个无来源数字、3 次第二轮空泛赞同。不能将缺来源直接推断为数字错误。");
  }

  {
    const s = p.slides.add();
    s.background.fill = C.bg;
    sectionTitle(s, 12, "“子任务完成”与“有可用文件”之间，还有四道关", "不要让内部进度词替代用户真正要拿到的结果。", C);
    const gates = [
      ["合并", "不同小组的结论归到一个主问题"],
      ["去重", "重复背景只写一次；分歧不能删掉"],
      ["制作", "生成真实文件，保留原生可编辑对象"],
      ["核对", "内容、来源、权限与最终文件逐项确认"],
    ];
    gates.forEach((g, i) => {
      const x = 72 + i * 286;
      addText(s, String(i + 1).padStart(2, "0"), x, 238, 68, 38, { fontSize: 17, bold: true, color: C.accent });
      addText(s, g[0], x, 296, 236, 56, { fontSize: 31, bold: true, color: C.ink });
      addText(s, g[1], x, 374, 238, 126, { fontSize: 19, color: C.muted });
      addRect(s, x, 528, 222, 5, { fill: i === 3 ? C.accent : C.rule });
    });
    addText(s, "默认回报：结论摘要 + 文件 + 限制；详细过程供展开。", 72, 574, 1080, 44, { fontSize: 23, bold: true, color: C.ink });
    footer(s, 12, "交付 / 完成判定", C.muted);
    notes(s, "D01 · SRC-M01：小组 D 记录用户误把子任务完成当整项结束；后续仍有合并、去重、文件制作和核对。");
  }

  {
    const s = p.slides.add();
    s.background.fill = C.paper;
    sectionTitle(s, 13, "两次旧进度，只能证明显示陈旧", "故障归因停在证据允许的位置。", C);
    addText(s, "2", 76, 218, 230, 180, { fontSize: 128, bold: true, color: C.accent });
    addText(s, "次", 258, 295, 74, 58, { fontSize: 31, bold: true, color: C.accent });
    addText(s, "返回页面后看到旧文字", 394, 258, 760, 64, { fontSize: 37, bold: true, color: C.ink });
    addText(s, "已观察到：状态显示需要刷新，并带上可核对的时间戳。", 396, 346, 740, 66, { fontSize: 21, color: C.muted });
    addRect(s, 76, 478, 1092, 132, { fill: C.ink });
    addText(s, "尚未证实", 102, 502, 175, 32, { fontSize: 17, bold: true, color: C.accentSoft });
    addText(s, "任务重跑或跨会话串扰", 288, 500, 760, 45, { fontSize: 29, bold: true, color: "#FFFFFF" });
    addText(s, "需要任务 ID、事件序列与发布记录再复现。", 290, 552, 730, 35, { fontSize: 18, color: "#D8DFE1" });
    footer(s, 13, "观察 / 归因边界", C.muted);
    notes(s, "D01 · SRC-M01：小组 D 记录两次返回页面后的陈旧进度。材料未确认重复执行或会话串扰；本页不把它们写成已证实根因。");
  }

  {
    const s = p.slides.add();
    s.background.fill = C.paper;
    sectionTitle(s, 14, "输入不足时暂停的是缺口，不是抹掉已做的工作", "5 / 48 个任务因输入缺失暂停；材料未列每个缺口的细节。", C);
    addText(s, "保留", 76, 230, 180, 39, { fontSize: 17, bold: true, color: C.good });
    addText(s, "已完成阶段、已有草稿、已确认来源", 76, 282, 1070, 58, { fontSize: 30, bold: true, color: C.ink });
    addRule(s, 76, 366, 1100, C.rule, 2);
    addText(s, "请求", 76, 406, 180, 39, { fontSize: 17, bold: true, color: C.accent });
    addText(s, "明确列出缺什么、为何需要、补齐后从哪一步继续", 76, 460, 1090, 80, { fontSize: 28, bold: true, color: C.ink });
    addText(s, "不能把“等待输入”伪装成最终交付，也不逼用户改要提纲。", 76, 582, 1040, 38, { fontSize: 21, color: C.muted });
    footer(s, 14, "恢复 / 缺输入", C.muted);
    notes(s, "D01 · SRC-M01：5 个任务因输入缺失暂停。行为建议为对该类故障的设计推导，并非试点已经实现的产品事实。");
  }

  {
    const s = p.slides.add();
    s.background.fill = C.paper;
    sectionTitle(s, 15, "超时后先辨认结果状态，再决定是否重试", "3 / 48 个任务超时且没有最终文件；本地超时不代表外部操作未发生。", C);
    addText(s, "未发出 / 明确失败", 76, 244, 360, 45, { fontSize: 23, bold: true, color: C.ink });
    addText(s, "→", 470, 242, 62, 45, { fontSize: 26, color: C.accent });
    addText(s, "沿原任务继续", 556, 240, 590, 50, { fontSize: 30, bold: true, color: C.good });
    addText(s, "保留幂等身份，只补未完成阶段。", 558, 302, 588, 43, { fontSize: 20, color: C.muted });
    addRule(s, 76, 378, 1090, C.rule, 2);
    addText(s, "已发出 / 回执未知", 76, 420, 360, 45, { fontSize: 23, bold: true, color: C.ink });
    addText(s, "→", 470, 418, 62, 45, { fontSize: 26, color: C.accent });
    addText(s, "先只读探测", 556, 416, 590, 50, { fontSize: 30, bold: true, color: C.accent });
    addText(s, "核对外部结果与本地阶段证据；确认未完成后再试。", 558, 478, 610, 64, { fontSize: 20, color: C.muted });
    addRect(s, 76, 586, 1090, 55, { fill: C.ink });
    addText(s, "用户 Stop 后，晚到结果也不能自动发布。", 100, 595, 1030, 37, { fontSize: 20, bold: true, color: "#FFFFFF" });
    footer(s, 15, "超时 / 只读探测与幂等恢复", C.muted);
    notes(s, "D01 · SRC-M01：3 个服务超时任务无最终文件。外部操作回执未知时需只读查询，不能假设未发生；本页为建议行为，不声称试点已实现。");
  }

  {
    const s = p.slides.add();
    s.background.fill = C.bg;
    sectionTitle(s, 16, "人工 v2 增补的风险，不能被 AI 从 v1 重做抹去", "局部再生成必须以当前版本的最新字节为底稿。", C);
    const stages = [
      ["v1", "原始草稿", C.muted],
      ["v2", "人工补风险", C.good],
      ["候选", "AI 局部修改", C.accent],
    ];
    stages.forEach((st, i) => {
      const x = 72 + i * 382;
      addText(s, st[0], x, 242, 110, 52, { fontSize: 34, bold: true, color: st[2] });
      addText(s, st[1], x, 312, 310, 64, { fontSize: 25, bold: true, color: C.ink });
      addRect(s, x, 390, 300, 5, { fill: st[2] });
      if (i < 2) addText(s, "→", x + 316, 296, 50, 55, { fontSize: 32, color: C.rule });
    });
    addText(s, "错误分支：v1 → AI 候选 → 覆盖 v2", 72, 478, 590, 53, { fontSize: 25, bold: true, color: C.accent });
    addText(s, "正确分支：v2 → 候选比较 → 人确认合并", 72, 550, 870, 53, { fontSize: 25, bold: true, color: C.good });
    footer(s, 16, "版本 / 手工编辑保护", C.muted);
    notes(s, "D01 · SRC-M01：材料记录有人在 v2 补一项风险，AI 从 v1 重做使风险丢失。不得夸大为不可恢复数据损失。");
  }

  {
    const s = p.slides.add();
    s.background.fill = C.ink;
    addText(s, "17 / 建议排期", 76, 50, 260, 34, { fontSize: 16, bold: true, color: C.accentSoft });
    addText(s, "先关掉重复交付和版本覆盖，", 76, 124, 1100, 73, { fontSize: 42, bold: true, color: "#FFFFFF" });
    addText(s, "再谈角色展示。", 76, 208, 1000, 80, { fontSize: 48, bold: true, color: C.accentSoft });
    addText(s, "3 名工程师  +  1 名设计师", 80, 335, 860, 52, { fontSize: 28, bold: true, color: "#FFFFFF" });
    addRule(s, 80, 410, 1086, "#607078", 2);
    addText(s, "第 1—2 周", 80, 450, 230, 34, { fontSize: 17, bold: true, color: C.accentSoft });
    addText(s, "同输入追踪文件、阶段和当前版本", 80, 500, 490, 71, { fontSize: 23, bold: true, color: "#FFFFFF" });
    addText(s, "第 3—4 周", 650, 450, 230, 34, { fontSize: 17, bold: true, color: C.accentSoft });
    addText(s, "结论与来源层级 → 角色可见性", 650, 500, 510, 71, { fontSize: 23, bold: true, color: "#FFFFFF" });
    addText(s, "这是建议顺序，不是已完成的里程碑。", 80, 624, 800, 33, { fontSize: 17, color: "#C4CED1" });
    footer(s, 17, "优先级 / 先可靠后展示", "#B7C0C5");
    notes(s, "D01 · SRC-M01：3 名工程师、1 名设计师；建议两周处理重复交付与版本覆盖，第 3 周处理结论和证据，第 4 周处理角色展示。材料没有证明这些里程碑已经完成。");
  }

  {
    const s = p.slides.add();
    s.background.fill = C.paper;
    sectionTitle(s, 18, "六个未决事项要有批准人，不能让幻灯片代替决定", "材料未提供成本；这里不制造 ROI。", C);
    const decisions = [
      ["角色动画", "是否推迟"], ["范围缩减", "谁能批准"], ["外部模型", "哪些输入可发送"],
      ["保真材料", "哪些表格/脚注必须留"], ["服务异常", "允许等待多久"], ["人工修订", "何时成为正式版"],
    ];
    decisions.forEach((d, i) => {
      const col = i % 2;
      const row = Math.floor(i / 2);
      const x = col ? 664 : 74;
      const y = 224 + row * 126;
      addText(s, d[0], x, y, 210, 39, { fontSize: 24, bold: true, color: C.ink });
      addText(s, d[1], x + 212, y + 2, 286, 38, { fontSize: 19, color: C.muted });
      addRule(s, x, y + 68, 502, C.rule, 1);
    });
    addText(s, "需要决定的是权责与边界，不是再加一页角色头像。", 74, 618, 1030, 38, { fontSize: 20, bold: true, color: C.accent });
    footer(s, 18, "未决 / 决策清单", C.muted);
    notes(s, "D01 · SRC-M01：六个决策问题按原材料逐项保留；成本资料不存在，不做 ROI。");
  }

  {
    const s = p.slides.add();
    s.background.fill = C.ink;
    addText(s, "19", 64, 48, 64, 32, { fontSize: 15, bold: true, color: C.accentSoft });
    addText(s, "可以进入开发，不能写成已经达标", 64, 100, 1090, 64, { fontSize: 40, bold: true, color: "#FFFFFF" });
    addText(s, "建议验收线", 66, 220, 220, 32, { fontSize: 17, bold: true, color: C.accentSoft });
    const criteria = ["12 个新任务中至少 11 个拿到有用文件", "关键事实 / 权限 / 覆盖错误为 0", "1 分钟内理解结果与边界", "由独立评审者放行，不由作者自评"];
    criteria.forEach((c, i) => {
      addText(s, `${i + 1}`, 68, 278 + i * 67, 36, 32, { fontSize: 18, bold: true, color: C.accent });
      addText(s, c, 120, 272 + i * 67, 680, 42, { fontSize: 22, color: "#FFFFFF" });
    });
    addText(s, "仍待回答", 876, 220, 230, 32, { fontSize: 17, bold: true, color: C.accentSoft });
    addText(s, "谁拥有最终裁决？\n哪些指标必须阻断发布？\n成本数据何时补齐？", 876, 278, 300, 170, { fontSize: 22, bold: true, color: "#FFFFFF" });
    addText(s, "当前状态：方向明确；结果尚未通过这组验收。", 876, 520, 300, 70, { fontSize: 18, color: C.accentSoft });
    footer(s, 19, "结论 / 验收与未决项", "#B7C0C5");
    notes(s, "Source: D01_workshop_notes.md。验收线是材料中的建议目标，明确未达成；成本缺失，不得补造 ROI。");
  }

  return {
    id: "D01",
    title: "Project M 工作坊复盘",
    presentation: p,
    requirements: {
      explicitTotalSlideCount: 19,
      requiredNativeTableOwnerSlides: [4],
      requiredNativeChartOwnerSlides: [3],
      materializeLiteralChartWorkbooks: true,
    },
  };
}

function buildD02() {
  const p = presentation();
  const C = {
    bg: "#F8F5EF", paper: "#FFFFFF", ink: "#211C2A", muted: "#706A78",
    accent: "#6543A5", accent2: "#C47D56", wash: "#EEE8F7", rule: "#DDD6E3",
    green: "#4E7B6C",
  };

  {
    const s = p.slides.add();
    s.background.fill = C.bg;
    addText(s, "清河实验室", 72, 54, 260, 32, { fontSize: 17, bold: true, color: C.accent });
    addText(s, "页伴", 72, 136, 400, 100, { fontSize: 72, bold: true, color: C.ink, autoFit: "none" });
    addText(s, "把散落材料变成\n能继续编辑的提案", 72, 258, 690, 128, { fontSize: 42, bold: true, color: C.ink, autoFit: "none" });
    addText(s, "面向项目协调者的产品发布提案 · 合成开发材料", 76, 424, 610, 40, { fontSize: 19, color: C.muted });
    addRect(s, 844, 90, 300, 430, { fill: C.accent, borderRadius: 150 });
    addText(s, "材料", 915, 188, 160, 48, { fontSize: 22, bold: true, color: "#FFFFFF", alignment: "center" });
    addRule(s, 916, 252, 158, "#BDA9DD", 2);
    addText(s, "提案", 915, 282, 160, 48, { fontSize: 22, bold: true, color: "#FFFFFF", alignment: "center" });
    addRule(s, 916, 346, 158, "#BDA9DD", 2);
    addText(s, "继续编辑", 886, 376, 220, 48, { fontSize: 22, bold: true, color: "#FFFFFF", alignment: "center" });
    addText(s, "不是替人设计一切，而是减少被格式打断的次数。", 72, 566, 760, 52, { fontSize: 22, color: C.accent });
    footer(s, 1, "PPT 标杆能力开发集 D02", C.muted);
    notes(s, "Source: D02_launch_notes.md（合成开发材料）。‘页伴’为本开发集中的合成产品，不代表真实公司或已发布产品。");
  }

  {
    const s = p.slides.add();
    s.background.fill = C.paper;
    sectionTitle(s, 2, "定位只回答一件事：减少从材料到提案的中断", "目标用户是项目协调者，不要求他们先成为专业设计师。", C);
    addText(s, "我们做", 72, 226, 180, 34, { fontSize: 16, bold: true, color: C.accent });
    addText(s, "TXT / MD / DOCX 正文与标准表格\n链接来源\n可编辑文档与演示文稿", 72, 276, 500, 180, { fontSize: 26, bold: true, color: C.ink });
    addText(s, "我们不声称", 684, 226, 240, 34, { fontSize: 16, bold: true, color: C.accent2 });
    addText(s, "复杂旧文件完美保真\n自动处理全部版权\n所有联网场景离线可用", 684, 276, 500, 180, { fontSize: 26, bold: true, color: C.ink });
    addRule(s, 624, 216, 2, C.rule, 2);
    addRect(s, 72, 520, 1112, 72, { fill: C.bg });
    addText(s, "一句话", 96, 538, 110, 30, { fontSize: 15, bold: true, color: C.accent });
    addText(s, "让协调者把判断花在内容上，而不是反复搬运格式。", 230, 532, 860, 40, { fontSize: 25, bold: true, color: C.ink });
    footer(s, 2, "定位 / 能力边界", C.muted);
    notes(s, "Source: D02_launch_notes.md。不得使用‘全格式无损’、‘零门槛’或‘行业第一’等材料未支持的表述。");
  }

  {
    const s = p.slides.add();
    s.background.fill = C.bg;
    sectionTitle(s, 3, "三种场景，共用同一条“继续做下去”的主线", "发布演示不列功能清单，而是让观众看到动作与结果。", C);
    const scenes = [
      { n: "01", title: "评审会", body: "会议记录 → 结论页\n来源留在备注与证据层", color: C.accent },
      { n: "02", title: "客户提案", body: "材料 → 结构化提案\n当场改第 3 页文案", color: C.accent2 },
      { n: "03", title: "内部培训", body: "讲义 → 教学演示\n手工改图后继续协作", color: C.green },
    ];
    scenes.forEach((scene, i) => {
      const x = 72 + i * 382;
      addText(s, scene.n, x, 224, 60, 32, { fontSize: 15, bold: true, color: scene.color });
      addText(s, scene.title, x, 276, 310, 52, { fontSize: 31, bold: true, color: C.ink });
      addText(s, scene.body, x, 352, 310, 112, { fontSize: 21, color: C.muted });
      addRect(s, x, 484, 306, 5, { fill: scene.color });
    });
    addText(s, "每个场景都要演示一次“用户改动没有被下一轮覆盖”。", 72, 558, 930, 46, { fontSize: 24, bold: true, color: C.ink });
    footer(s, 3, "故事板 / 三个用户场景", C.muted);
    notes(s, "Source: D02_launch_notes.md。三类场景与演示动作来自材料；此页是发布故事板，不声称功能已在生产环境通过验收。");
  }

  {
    const s = p.slides.add();
    s.background.fill = C.paper;
    sectionTitle(s, 4, "演示一：从会议笔记到能讨论的客户提案", "先看结论，再追来源；用户修改仍然是当前版本。", C);
    const steps = [
      ["1", "放入笔记", "保留正文、标准表格与来源链接"],
      ["2", "生成提案", "标题写判断，不复制台账标签"],
      ["3", "改第 3 页", "模型只改目标页，不重做整套"],
      ["4", "回到对话", "继续围绕刚才那一版协作"],
    ];
    steps.forEach((step, i) => {
      const y = 218 + i * 94;
      addCircle(s, 76, y + 3, 44, i === 2 ? C.accent2 : C.accent);
      addText(s, step[0], 88, y + 12, 20, 20, { fontSize: 14, bold: true, color: "#FFFFFF", alignment: "center" });
      addText(s, step[1], 144, y, 260, 40, { fontSize: 25, bold: true, color: C.ink });
      addText(s, step[2], 434, y, 700, 48, { fontSize: 19, color: C.muted });
    });
    addRect(s, 1000, 496, 160, 90, { fill: C.wash, borderRadius: 45 });
    addText(s, "可编辑", 1022, 521, 116, 36, { fontSize: 20, bold: true, color: C.accent, alignment: "center" });
    footer(s, 4, "演示 / 用户动作链", C.muted);
    notes(s, "Source: D02_launch_notes.md。演示动作必须包括改第 3 页文字、手工编辑图表并回到对话；本页只描述第一个完整动作链。");
  }

  {
    const s = p.slides.add();
    s.background.fill = C.bg;
    sectionTitle(s, 5, "编辑不是“导出后自便”，而是协作链中的一环", "手工编辑后的最新字节继续成为事实版本。", C);
    const items = [
      ["A", "AI 初稿", "结构与原生对象可继续编辑"],
      ["B", "人手修正", "文案、图表与排版成为当前版"],
      ["C", "局部再生成", "只生成候选，不覆盖 B"],
      ["D", "确认合并", "新版本沿用同一作品 owner"],
    ];
    items.forEach((it, i) => {
      const x = 72 + i * 286;
      addText(s, it[0], x, 236, 48, 34, { fontSize: 16, bold: true, color: i === 1 ? C.accent2 : C.accent });
      addText(s, it[1], x, 286, 242, 48, { fontSize: 27, bold: true, color: C.ink });
      addText(s, it[2], x, 348, 234, 110, { fontSize: 18, color: C.muted });
      if (i < 3) addRule(s, x + 234, 316, 40, C.rule, 2);
    });
    addRect(s, 72, 526, 1112, 74, { fill: C.paper });
    addText(s, "发布演示的关键瞬间", 96, 545, 260, 32, { fontSize: 16, bold: true, color: C.accent });
    addText(s, "手工改图后回到对话，AI 能认出并继续刚才那一版。", 374, 538, 740, 42, { fontSize: 24, bold: true, color: C.ink });
    footer(s, 5, "演示 / 版本连续性", C.muted);
    notes(s, "Source: D02_launch_notes.md。此页明确理想产品行为，不等同于当前候选已经通过真实 PowerPoint 保存—重开测试。");
  }

  {
    const s = p.slides.add();
    s.background.fill = C.paper;
    sectionTitle(s, 6, "20 人报名，14 人完成首轮；复用信号存在，但没有对照组", "10 人在第二周再次使用，占首轮完成者 71%；占全部招募者 50%。", C);
    addChart(s, "bar", {
      position: { left: 70, top: 220, width: 720, height: 350 },
      title: "试用参与（人数）",
      categories: ["招募", "完成首轮", "第二周复用"],
      series: [{ name: "人数", values: [20, 14, 10], fill: C.accent, points: [{ idx: 1, fill: C.accent2 }, { idx: 2, fill: C.green }] }],
      barOptions: { direction: "column", grouping: "clustered", gapWidth: 65 },
      hasLegend: false,
      xAxis: { line: { style: "solid", fill: C.rule, width: 1 }, textStyle: { fill: C.ink, fontSize: 14 } },
      yAxis: { min: 0, max: 22, majorUnit: 5, majorGridlines: { style: "solid", fill: C.rule, width: 1 }, textStyle: { fill: C.muted, fontSize: 12 } },
      dataLabels: { showValue: true, position: "outEnd", textStyle: { fill: C.ink, fontSize: 15, bold: true } },
      chartFill: C.paper, plotAreaFill: C.paper,
      chartLine: { fill: "none", width: 0 }, plotAreaLine: { fill: "none", width: 0 },
    });
    addText(s, "14 / 20", 886, 238, 260, 62, { fontSize: 48, bold: true, color: C.accent2 });
    addText(s, "完成首轮 · 70%", 890, 302, 270, 34, { fontSize: 18, bold: true, color: C.ink });
    addText(s, "10 / 14", 886, 390, 260, 62, { fontSize: 48, bold: true, color: C.green });
    addText(s, "完成者中第二周复用 · 71%", 890, 454, 294, 52, { fontSize: 18, bold: true, color: C.ink });
    addText(s, "没有对照组，不能把复用直接归因为产品。", 890, 538, 290, 48, { fontSize: 17, color: C.muted });
    footer(s, 6, "证据 / 定向试用", C.muted);
    notes(s, "Source: D02_launch_notes.md。20 人招募，14 人完成首轮，6 人退出，10 名完成者在第二周复用。70%=14/20；71%=10/14（四舍五入）；50%=10/20。没有对照组，不做因果声明。");
  }

  {
    const s = p.slides.add();
    s.background.fill = C.bg;
    sectionTitle(s, 7, "品牌只做识别，不抢内容的声音", "小面积深紫强调；暖白与白为主。", C);
    addRect(s, 72, 230, 310, 260, { fill: C.accent, borderRadius: 155 });
    addText(s, "页伴", 140, 318, 174, 70, { fontSize: 45, bold: true, color: "#FFFFFF", alignment: "center" });
    addText(s, "文字标识", 166, 392, 130, 28, { fontSize: 14, color: "#D8C9EF", alignment: "center" });
    addText(s, "保留", 474, 232, 190, 34, { fontSize: 16, bold: true, color: C.accent });
    addText(s, "深紫小面积强调\n暖白 / 白色底\n清晰留白与原生对象", 474, 282, 320, 142, { fontSize: 24, bold: true, color: C.ink });
    addText(s, "禁止", 850, 232, 190, 34, { fontSize: 16, bold: true, color: C.accent2 });
    addText(s, "大面积渐变\n拟物按钮\n阴影卡片铺满页面", 850, 282, 320, 142, { fontSize: 24, bold: true, color: C.ink });
    addRule(s, 474, 474, 696, C.rule, 2);
    addText(s, "未确认的价格、发布日期与客户名称：标“待定”或直接省略。", 474, 512, 696, 64, { fontSize: 21, color: C.muted });
    footer(s, 7, "视觉方向 / 品牌边界", C.muted);
    notes(s, "Source: D02_launch_notes.md。Logo 只允许文字形式。价格、发布日期与客户名称均未确认。");
  }

  editorialPage(p, C, 8,
    "每周做两三次材料的人，最怕的是中途被格式打断",
    "受众是小团队的项目协调者，不是专业平面设计师。",
    { label: "工作起点", headline: "材料是散的", body: "会议记录、文本与标准表格先被整理；来源仍可追溯，团队不必从空白页重新搬运。" },
    { label: "工作终点", headline: "提案能继续", body: "拿到可编辑文档与演示后，仍可改文案和数据，再回到对话继续围绕当前版本工作。" },
    "D02 · SRC-L01：目标用户每周准备 2–3 次材料，前三场景为复盘、客户提案、内部培训。");

  {
    const s = p.slides.add();
    s.background.fill = C.paper;
    sectionTitle(s, 9, "发布演示不能停在一张漂亮的生成结果", "用同一份项目记录，连续完成四个可核验动作。", C);
    const steps = [
      ["01", "记录 → 提案", "输入真实材料，保留来源"],
      ["02", "改第 3 页", "只改变目标句，不重排其他页"],
      ["03", "手改图表", "保存并重开，数据仍可编辑"],
      ["04", "返回对话", "继续刚才的作品与最新字节"],
    ];
    steps.forEach((step, i) => {
      const y = 222 + i * 98;
      addText(s, step[0], 76, y, 70, 40, { fontSize: 18, bold: true, color: i === 3 ? C.accent2 : C.accent });
      addText(s, step[1], 164, y - 4, 300, 46, { fontSize: 29, bold: true, color: C.ink });
      addText(s, step[2], 516, y + 2, 624, 42, { fontSize: 20, color: C.muted });
      if (i < 3) addRule(s, 164, y + 62, 966, C.rule, 1);
    });
    footer(s, 9, "演示 / 连续任务", C.muted);
    notes(s, "D02 · SRC-L01：推荐演示的四个动作逐项保留；这是一份产品发布演示脚本，不代表澜台候选已通过真实 App 端到端验收。");
  }

  editorialPage(p, C, 10,
    "发布叙事不以模型、向量库与插件数开场",
    "技术能力可以做补充证据，但不能替代用户知道自己能完成什么。",
    { label: "技术视角", headline: "能力如何实现", body: "模型、检索与插件是实现细节；材料没有数量、质量对照或可发布指标，不做夸耀。" },
    { label: "用户视角", headline: "工作如何继续", body: "主张聚焦“减少从资料到提案的中断”，再用三种演示与明确限制支撑。" },
    "D02 · SRC-L01：技术与销售对定位有争议；本页按材料要求裁决发布叙事，不补造技术参数。");

  {
    const s = p.slides.add();
    s.background.fill = C.bg;
    sectionTitle(s, 11, "先让已有内测者试新流程，再决定何时开放候补", "发布顺序有依据；价格、日期与客户名单没有。", C);
    addText(s, "第一步", 76, 242, 160, 37, { fontSize: 17, bold: true, color: C.accent });
    addText(s, "邀请已有内测者", 76, 296, 475, 63, { fontSize: 35, bold: true, color: C.ink });
    addText(s, "验证三类场景与编辑保存；收集失败路径，不只选成功画面。", 76, 384, 466, 122, { fontSize: 21, color: C.muted });
    addRect(s, 622, 232, 2, 326, { fill: C.rule });
    addText(s, "第二步", 688, 242, 160, 37, { fontSize: 17, bold: true, color: C.accent2 });
    addText(s, "根据反馈开放候补", 688, 296, 480, 63, { fontSize: 35, bold: true, color: C.ink });
    addText(s, "价格、发布日期、客户引用都需另行确认；页面不借不存在的客户 logo 建立信任。", 688, 384, 480, 130, { fontSize: 21, color: C.muted });
    addText(s, "待确认：价格 / 日期 / 客户名单", 76, 592, 1000, 40, { fontSize: 23, bold: true, color: C.accent });
    footer(s, 11, "发布 / 次序与未决", C.muted);
    notes(s, "D02 · SRC-L01：先邀请已有内测者，依据反馈再开放候补。价格、日期、客户名单均未确认。");
  }

  {
    const s = p.slides.add();
    s.background.fill = C.accent;
    addText(s, "12", 64, 50, 64, 32, { fontSize: 15, bold: true, color: "#D8C9EF" });
    addText(s, "下一步不是加更多功能，\n而是让三次演示可信", 64, 112, 760, 118, { fontSize: 43, bold: true, color: "#FFFFFF", autoFit: "none" });
    const next = [
      ["01", "完整跑通三场景", "评审、提案、培训各自从原始材料开始"],
      ["02", "验证编辑保存", "改文字与图表，保存、重开、继续协作"],
      ["03", "补齐发布条件", "价格 / 日期 / 客户引用必须获得确认"],
    ];
    next.forEach((n, i) => {
      const y = 296 + i * 92;
      addText(s, n[0], 68, y, 56, 30, { fontSize: 14, bold: true, color: "#D8C9EF" });
      addText(s, n[1], 150, y - 2, 300, 36, { fontSize: 24, bold: true, color: "#FFFFFF" });
      addText(s, n[2], 486, y - 2, 630, 48, { fontSize: 18, color: "#E6DDF3" });
    });
    addText(s, "发布裁决：叙事已成形；真实产品验收与市场信息仍未完成。", 68, 598, 1040, 40, { fontSize: 21, bold: true, color: "#FFFFFF" });
    footer(s, 12, "结论 / 上线前条件", "#D8C9EF");
    notes(s, "Source: D02_launch_notes.md。三套演示与未确认信息来自材料；不得把本开发作品当成真实发布批准。");
  }

  return {
    id: "D02",
    title: "页伴产品发布提案",
    presentation: p,
    requirements: {
      explicitTotalSlideCount: 12,
      requiredNativeTableOwnerSlides: [],
      requiredNativeChartOwnerSlides: [6],
      materializeLiteralChartWorkbooks: true,
    },
  };
}

function buildD03() {
  const p = presentation();
  const C = {
    bg: "#F5F2EA", paper: "#FFFFFF", ink: "#102A36", muted: "#5F7178",
    accent: "#0D7C75", orange: "#D06A3A", blue: "#3E6E8C", wash: "#DCECE8",
    rule: "#D4DBD9", danger: "#B7433E",
  };

  {
    const s = p.slides.add();
    s.background.fill = C.ink;
    addText(s, "2025 年度经营复盘 · 合成开发数据", 72, 58, 620, 34, { fontSize: 16, bold: true, color: "#8FC8C2" });
    addText(s, "收入增长，\n经营贡献却在 Q4 回落", 72, 164, 870, 150, { fontSize: 52, bold: true, color: "#FFFFFF", autoFit: "none" });
    addText(s, "管理层需要回答：增长是否真正转化为经营贡献？", 76, 360, 830, 42, { fontSize: 22, color: "#C7D5D9" });
    addText(s, "600", 74, 500, 180, 66, { fontSize: 54, bold: true, color: "#FFFFFF" });
    addText(s, "全年收入 · 万元", 78, 564, 210, 28, { fontSize: 15, color: "#8FC8C2" });
    addText(s, "121", 356, 500, 180, 66, { fontSize: 54, bold: true, color: "#FFFFFF" });
    addText(s, "全年经营贡献 · 万元", 360, 564, 230, 28, { fontSize: 15, color: "#8FC8C2" });
    addText(s, "25", 660, 500, 180, 66, { fontSize: 54, bold: true, color: C.orange });
    addText(s, "Q4 经营贡献 · 万元", 664, 564, 230, 28, { fontSize: 15, color: "#F0BEA3" });
    footer(s, 1, "PPT 标杆能力开发集 D03", "#A9BBC0");
    notes(s, "Source: D03_quarter_data.json 与 D03_brief.md（合成开发材料）。单位：万元；期间：2025Q1–Q4。全年收入 120+145+160+175=600；全年成本 95+108+126+150=479；全年经营贡献 121。");
  }

  {
    const s = p.slides.add();
    s.background.fill = C.bg;
    sectionTitle(s, 2, "答案：全年贡献为正，但 Q4 的增收没有转化为更多贡献", "Q4 收入较 Q3 增长 9.4%，经营贡献却从 34 万元降至 25 万元。", C);
    addText(s, "+9.4%", 76, 238, 310, 90, { fontSize: 62, bold: true, color: C.accent });
    addText(s, "Q4 收入环比", 80, 326, 270, 34, { fontSize: 18, bold: true, color: C.ink });
    addText(s, "−9", 478, 238, 310, 90, { fontSize: 62, bold: true, color: C.orange });
    addText(s, "Q4 经营贡献环比 · 万元", 482, 326, 320, 34, { fontSize: 18, bold: true, color: C.ink });
    addText(s, "121", 882, 238, 250, 90, { fontSize: 62, bold: true, color: C.blue });
    addText(s, "全年经营贡献 · 万元", 886, 326, 280, 34, { fontSize: 18, bold: true, color: C.ink });
    addRule(s, 76, 405, 1086, C.rule, 2);
    addText(s, "管理含义", 76, 448, 190, 34, { fontSize: 16, bold: true, color: C.accent });
    addText(s, "年末贡献承压；先查成本构成与收入质量，再定扩张节奏。", 76, 494, 1040, 80, { fontSize: 27, bold: true, color: C.ink });
    addText(s, "注意：现有数据不能解释 Q4 成本上升的原因。", 76, 602, 740, 30, { fontSize: 17, color: C.danger });
    footer(s, 2, "执行摘要 / 回答核心问题", C.muted);
    notes(s, "Source: D03_quarter_data.json。Q4 收入环比=(175-160)/160=9.375%，显示为 9.4%；Q4 经营贡献变化=25-34=-9 万元。原因未知，需调查。");
  }

  {
    const s = p.slides.add();
    s.background.fill = C.paper;
    sectionTitle(s, 3, "收入与成本同步上行，Q4 间距明显收窄", "单位：万元；原生折线图，保留图表数据供后续编辑。", C);
    addChart(s, "line", {
      position: { left: 76, top: 206, width: 1080, height: 390 },
      title: "2025 季度收入与成本",
      categories: ["Q1", "Q2", "Q3", "Q4"],
      series: [
        { name: "收入", values: [120, 145, 160, 175], line: { style: "solid", fill: C.accent, width: 4 }, marker: { symbol: "circle", size: 8 } },
        { name: "成本", values: [95, 108, 126, 150], line: { style: "solid", fill: C.orange, width: 4 }, marker: { symbol: "circle", size: 8 } },
      ],
      lineOptions: { grouping: "standard", smooth: false },
      hasLegend: true,
      legend: { position: "bottom", overlay: false, textStyle: { fill: C.ink, fontSize: 14 } },
      xAxis: { line: { style: "solid", fill: C.rule, width: 1 }, textStyle: { fill: C.ink, fontSize: 14 } },
      yAxis: { min: 80, max: 180, majorUnit: 20, numberFormatCode: "0", title: "万元", majorGridlines: { style: "solid", fill: C.rule, width: 1 }, textStyle: { fill: C.muted, fontSize: 12 } },
      chartFill: C.paper, plotAreaFill: C.paper,
      chartLine: { fill: "none", width: 0 }, plotAreaLine: { fill: "none", width: 0 },
    });
    addText(s, "Q4 收入 +15；成本 +24", 848, 178, 320, 30, { fontSize: 16, bold: true, color: C.danger, alignment: "right" });
    footer(s, 3, "趋势 / 收入与成本", C.muted);
    notes(s, "Source: D03_quarter_data.json。收入 [120,145,160,175]；成本 [95,108,126,150]；单位万元。Q4 对 Q3：收入 +15，成本 +24。");
  }

  {
    const s = p.slides.add();
    s.background.fill = C.bg;
    sectionTitle(s, 4, "经营贡献在 Q2 见顶，Q4 回到 Q1 水平", "全年总额 121 万元掩盖了下半年连续回落。", C);
    addChart(s, "bar", {
      position: { left: 78, top: 218, width: 830, height: 350 },
      title: "季度经营贡献（万元）",
      categories: ["Q1", "Q2", "Q3", "Q4"],
      series: [{ name: "经营贡献", values: [25, 37, 34, 25], fill: C.accent, points: [{ idx: 3, fill: C.orange }] }],
      barOptions: { direction: "column", grouping: "clustered", gapWidth: 58 },
      hasLegend: false,
      xAxis: { line: { style: "solid", fill: C.rule, width: 1 }, textStyle: { fill: C.ink, fontSize: 14 } },
      yAxis: { min: 0, max: 42, majorUnit: 10, majorGridlines: { style: "solid", fill: C.rule, width: 1 }, textStyle: { fill: C.muted, fontSize: 12 } },
      dataLabels: { showValue: true, position: "outEnd", textStyle: { fill: C.ink, fontSize: 15, bold: true } },
      chartFill: C.bg, plotAreaFill: C.bg,
      chartLine: { fill: "none", width: 0 }, plotAreaLine: { fill: "none", width: 0 },
    });
    addText(s, "−32%", 978, 252, 190, 70, { fontSize: 54, bold: true, color: C.orange });
    addText(s, "Q4 较 Q2 峰值", 982, 326, 190, 32, { fontSize: 17, bold: true, color: C.ink });
    addText(s, "37 → 25", 978, 414, 190, 46, { fontSize: 30, bold: true, color: C.ink });
    addText(s, "贡献回到 Q1 水平", 982, 468, 210, 58, { fontSize: 17, color: C.muted });
    footer(s, 4, "趋势 / 经营贡献", C.muted);
    notes(s, "Source: D03_quarter_data.json。经营贡献=收入-成本，分别为 [25,37,34,25] 万元。Q4 较 Q2=(25-37)/37=-32.4%，显示为 -32%。");
  }

  {
    const s = p.slides.add();
    s.background.fill = C.paper;
    sectionTitle(s, 5, "季度台账显示：Q4 是收入最高、贡献并列最低的一季", "数值来自同一份合成数据；表格为原生可编辑对象。", C);
    const table = s.tables.add({
      rows: 6, columns: 5, left: 78, top: 224, width: 1110, height: 322,
      columnWidths: [260, 212, 212, 212, 212],
      values: [
        ["指标（万元，客户数除外）", "Q1", "Q2", "Q3", "Q4"],
        ["收入", 120, 145, 160, 175],
        ["成本", 95, 108, 126, 150],
        ["经营贡献", 25, 37, 34, 25],
        ["客户数（个）", 18, 21, 24, 25],
        ["贡献 / 收入", "20.8%", "25.5%", "21.3%", "14.3%"],
      ],
    });
    styleTable(table, 6, 5, C, { headerFill: C.ink, bodyFill: C.paper, fontSize: 16 });
    table.cells.block({ row: 1, column: 4, rowCount: 5, columnCount: 1 }).assign({ fill: "#F5E5DC" });
    table.cells.block({ row: 3, column: 1, rowCount: 1, columnCount: 4 }).assign({ fill: C.wash, textStyle: { typeface: FONT, fontSize: 16, color: C.ink, bold: true } });
    addText(s, "Q4 贡献 / 收入降至 14.3%", 78, 582, 520, 38, { fontSize: 23, bold: true, color: C.orange });
    addText(s, "该比率是经营观察，不等于会计口径利润率。", 638, 586, 548, 34, { fontSize: 17, color: C.muted, alignment: "right" });
    footer(s, 5, "明细 / 季度对照", C.muted);
    notes(s, "Source: D03_quarter_data.json。经营贡献/收入：25/120=20.8%，37/145=25.5%，34/160=21.3%，25/175=14.3%（显示到 1 位小数）。此比率仅按材料定义，不改写为会计利润率。");
  }

  {
    const s = p.slides.add();
    s.background.fill = C.bg;
    sectionTitle(s, 6, "Q4 的问题不是“没有增长”，而是增量被更快的成本吞没", "与 Q3 相比：收入 +15 万元，成本 +24 万元，经营贡献 −9 万元。", C);
    const bars = [
      { label: "收入增量", value: "+15", width: 300, color: C.accent, y: 248 },
      { label: "成本增量", value: "+24", width: 480, color: C.orange, y: 356 },
      { label: "贡献变化", value: "−9", width: 180, color: C.danger, y: 464 },
    ];
    bars.forEach((b) => {
      addText(s, b.label, 78, b.y, 180, 40, { fontSize: 19, bold: true, color: C.ink });
      addRect(s, 270, b.y + 2, b.width, 42, { fill: b.color });
      addText(s, b.value, 288 + b.width, b.y - 1, 110, 44, { fontSize: 25, bold: true, color: b.color });
    });
    addRect(s, 892, 236, 280, 272, { fill: C.paper });
    addText(s, "不能从现有数据断言", 918, 262, 230, 34, { fontSize: 16, bold: true, color: C.danger });
    addText(s, "• 成本上升的具体原因\n• 客户质量或留存变化\n• 扩张是否应该立即停止", 918, 320, 226, 138, { fontSize: 19, color: C.ink });
    addText(s, "下一步：拆成本与收入结构。", 78, 582, 640, 42, { fontSize: 25, bold: true, color: C.ink });
    footer(s, 6, "诊断 / Q4 增量", C.muted);
    notes(s, "Source: D03_quarter_data.json。Q4 对 Q3：收入 175-160=+15；成本 150-126=+24；经营贡献 25-34=-9。材料没有成本分项，不解释原因。");
  }

  {
    const s = p.slides.add();
    s.background.fill = C.paper;
    sectionTitle(s, 7, "全年 600 万元收入，留下 121 万元经营贡献", "这是加总结果，不是现金流、净利润或经审计指标。", C);
    addText(s, "600", 78, 224, 260, 84, { fontSize: 68, bold: true, color: C.ink });
    addText(s, "收入", 82, 308, 180, 34, { fontSize: 18, bold: true, color: C.muted });
    addText(s, "− 479", 446, 224, 260, 84, { fontSize: 68, bold: true, color: C.orange });
    addText(s, "成本", 452, 308, 180, 34, { fontSize: 18, bold: true, color: C.muted });
    addText(s, "= 121", 830, 224, 300, 84, { fontSize: 68, bold: true, color: C.accent });
    addText(s, "经营贡献", 838, 308, 180, 34, { fontSize: 18, bold: true, color: C.muted });
    addRule(s, 78, 388, 1036, C.rule, 2);
    addText(s, "20.2%", 78, 444, 280, 72, { fontSize: 54, bold: true, color: C.blue });
    addText(s, "全年贡献 / 收入", 82, 518, 280, 34, { fontSize: 18, bold: true, color: C.ink });
    addText(s, "解释边界", 496, 446, 180, 34, { fontSize: 16, bold: true, color: C.accent });
    addText(s, "只能说明这组材料中的收入减成本结果；不能替代完整损益表。", 496, 494, 630, 72, { fontSize: 23, color: C.ink });
    footer(s, 7, "全年 / 加总关系", C.muted);
    notes(s, "Source: D03_quarter_data.json。全年收入 600，成本 479，经营贡献 121，单位万元；121/600=20.17%，显示为 20.2%。材料将经营贡献定义为收入减成本。");
  }

  {
    const s = p.slides.add();
    s.background.fill = C.bg;
    sectionTitle(s, 8, "客户数从 18 增至 25，但不能据此推断留存或 LTV", "该指标只记录每季客户数，没有客户身份、活跃度或流失信息。", C);
    addChart(s, "line", {
      position: { left: 78, top: 226, width: 820, height: 330 },
      title: "季度客户数（个）",
      categories: ["Q1", "Q2", "Q3", "Q4"],
      series: [{ name: "客户数", values: [18, 21, 24, 25], line: { style: "solid", fill: C.blue, width: 4 }, marker: { symbol: "circle", size: 8 } }],
      lineOptions: { grouping: "standard", smooth: false },
      hasLegend: false,
      xAxis: { line: { style: "solid", fill: C.rule, width: 1 }, textStyle: { fill: C.ink, fontSize: 14 } },
      yAxis: { min: 16, max: 27, majorUnit: 2, numberFormatCode: "0", majorGridlines: { style: "solid", fill: C.rule, width: 1 }, textStyle: { fill: C.muted, fontSize: 12 } },
      dataLabels: { showValue: true, position: "outEnd", textStyle: { fill: C.ink, fontSize: 14, bold: true } },
      chartFill: C.bg, plotAreaFill: C.bg,
      chartLine: { fill: "none", width: 0 }, plotAreaLine: { fill: "none", width: 0 },
    });
    addText(s, "+7", 978, 248, 180, 72, { fontSize: 58, bold: true, color: C.blue });
    addText(s, "Q1 → Q4 客户数", 982, 324, 200, 34, { fontSize: 17, bold: true, color: C.ink });
    addText(s, "仍缺：", 978, 414, 160, 30, { fontSize: 16, bold: true, color: C.danger });
    addText(s, "新增 / 留存拆分\n客单价与收入结构\n客户活跃与获取成本", 978, 460, 220, 120, { fontSize: 18, color: C.ink });
    footer(s, 8, "客户 / 数量边界", C.muted);
    notes(s, "Source: D03_quarter_data.json 与 D03_brief.md。客户数 [18,21,24,25]。材料明确禁止从客户数推断活跃、留存或 LTV。");
  }

  {
    const s = p.slides.add();
    s.background.fill = C.paper;
    sectionTitle(s, 9, "把“为什么”留在调查清单，不把猜测写成结论", "当前数据足够定位问题，不足以解释问题。", C);
    const qs = [
      ["成本结构", "Q4 增量 24 万元来自哪些科目？一次性还是持续性？"],
      ["收入质量", "新增收入来自新客、扩单还是价格变化？"],
      ["客户构成", "客户数增加 1 个，为什么收入增加 15 万元？"],
      ["行动阈值", "贡献 / 收入跌到什么水平需要调整扩张节奏？"],
    ];
    qs.forEach((q, i) => {
      const y = 218 + i * 92;
      addText(s, String(i + 1).padStart(2, "0"), 78, y, 50, 34, { fontSize: 15, bold: true, color: C.accent });
      addText(s, q[0], 150, y - 2, 230, 40, { fontSize: 24, bold: true, color: C.ink });
      addText(s, q[1], 410, y - 1, 750, 48, { fontSize: 19, color: C.muted });
      if (i < 3) addRule(s, 150, y + 66, 1010, C.rule, 1);
    });
    footer(s, 9, "调查 / 下一组证据", C.muted);
    notes(s, "Source: D03_brief.md。问题是基于已知缺口提出的调查项，不代表任何成本原因已经被证实。");
  }

  {
    const s = p.slides.add();
    s.background.fill = C.ink;
    addText(s, "10", 64, 50, 64, 32, { fontSize: 15, bold: true, color: "#8FC8C2" });
    addText(s, "今天可以做的决定", 64, 106, 580, 60, { fontSize: 40, bold: true, color: "#FFFFFF" });
    addText(s, "继续增长，但把 Q4 贡献回落设为下一轮经营审查的第一问题。", 64, 190, 1060, 78, { fontSize: 29, bold: true, color: "#FFFFFF" });
    addText(s, "可以确认", 66, 330, 240, 34, { fontSize: 16, bold: true, color: "#8FC8C2" });
    addText(s, "全年贡献为正；Q4 收入继续增长；贡献跌回 25 万元。", 66, 376, 470, 112, { fontSize: 22, color: "#FFFFFF" });
    addText(s, "不能确认", 610, 330, 240, 34, { fontSize: 16, bold: true, color: "#F0BEA3" });
    addText(s, "成本上升原因、客户留存、LTV，以及扩张的最优速度。", 610, 376, 470, 112, { fontSize: 22, color: "#FFFFFF" });
    addRule(s, 66, 528, 1020, "#35505B", 2);
    addText(s, "下次会议输入", 66, 564, 190, 30, { fontSize: 15, bold: true, color: "#8FC8C2" });
    addText(s, "Q4 成本明细 + 收入构成 + 客户新增 / 留存拆分", 280, 556, 806, 42, { fontSize: 23, bold: true, color: "#FFFFFF" });
    footer(s, 10, "决策 / 结论与边界", "#A9BBC0");
    notes(s, "Source: D03_quarter_data.json 与 D03_brief.md。此页只裁决现有数据支持的结论；下一次会议所需输入为建议，不声称已获得。");
  }

  return {
    id: "D03",
    title: "2025 年度经营复盘",
    presentation: p,
    requirements: {
      explicitTotalSlideCount: 10,
      requiredNativeTableOwnerSlides: [5],
      requiredNativeChartOwnerSlides: [3, 4, 8],
      materializeLiteralChartWorkbooks: true,
    },
  };
}

function buildD04() {
  const p = presentation();
  const C = {
    bg: "#F5F0E6", paper: "#FFFCF6", ink: "#25302C", muted: "#596A62",
    accent: "#285A50", light: "#DDE9E1", rule: "#C8D3CA", caution: "#914C36",
  };
  const source = "SRC-R01 · 合成测试材料 · 非真实研究发表";
  const base = (number, title, subtitle = "") => {
    const s = p.slides.add();
    s.background.fill = number % 2 ? C.paper : C.bg;
    sectionTitle(s, number, title, subtitle, C);
    footer(s, number, source, C.muted);
    return s;
  };
  const note = (s, detail) => notes(s, `Source: D04_research_notes.md (${source})。${detail}`);
  const row = (s, y, index, heading, body) => {
    addText(s, index, 76, y, 72, 52, { fontSize: 34, bold: true, color: C.accent });
    addText(s, heading, 174, y, 350, 42, { fontSize: 26, bold: true, color: C.ink });
    addText(s, body, 542, y, 626, 74, { fontSize: 19, color: C.ink });
    addRule(s, 174, y + 82, 996, C.rule, 1);
  };

  {
    const s = p.slides.add();
    s.background.fill = C.ink;
    addRect(s, 914, 0, 366, 720, { fill: C.accent });
    addText(s, "研究答辩  /  合成测试", 72, 72, 600, 32, { fontSize: 16, bold: true, color: "#A9CBBE" });
    addText(s, "课堂提示如何组织，\n比‘哪班更聪明’更值得研究", 72, 158, 770, 170,
      { fontSize: 44, bold: true, color: "#FFFFFF" });
    addText(s, "概念定义 · 反例 · 简短练习", 76, 370, 700, 42,
      { fontSize: 24, color: "#E6EDE7" });
    addRule(s, 76, 463, 742, "#668078", 2);
    addText(s, "48 名学生  /  两班各 24 人  /  非随机比较", 76, 497, 730, 42,
      { fontSize: 21, color: "#E6EDE7" });
    addText(s, "SRC-R01 · 合成测试材料，非真实研究发表", 76, 625, 720, 28,
      { fontSize: 15, color: "#A9CBBE" });
    addText(s, "+15", 962, 214, 250, 88, { fontSize: 62, bold: true, color: "#FFFFFF" });
    addText(s, "A 班组平均变化", 966, 310, 258, 34, { fontSize: 17, color: "#DDE9E1" });
    addText(s, "+21", 962, 400, 250, 88, { fontSize: 62, bold: true, color: "#FFFFFF" });
    addText(s, "B 班组平均变化", 966, 496, 258, 34, { fontSize: 17, color: "#DDE9E1" });
    note(s, "标题提出材料组织的研究问题；+15 和 +21 仅为组均值变化，不能支持因果归因。");
  }

  {
    const s = base(2, "先看证据，再谈方法", "本答辩分六步推进；描述性结论与局限同等醒目。");
    const items = [
      ["01", "研究问题", "定义、反例与练习如何组合"],
      ["02", "研究设计", "两班 48 人；非随机分组"],
      ["03", "描述性数据", "课前 / 课后组平均与变化"],
      ["04", "质性与观察", "两条匿名引语及课堂观察"],
      ["05", "结论与局限", "贡献主张及不能下的结论"],
      ["06", "下一步", "随机或交叉设计、延迟测验"],
    ];
    items.forEach(([n, title, detail], i) => {
      const y = 218 + i * 66;
      addText(s, n, 88, y, 70, 34, { fontSize: 17, bold: true, color: C.accent });
      addText(s, title, 204, y - 2, 320, 38, { fontSize: 23, bold: true, color: C.ink });
      addText(s, detail, 554, y, 580, 36, { fontSize: 18, color: C.muted });
      if (i < 5) addRule(s, 204, y + 49, 924, C.rule, 1);
    });
    note(s, "答辩要求：研究问题、方法、描述性数据、质性观察、局限和下一步均需出现。");
  }

  {
    const s = base(3, "研究问题：三种材料如何组合？", "只比较材料组织方式，不评判某一种学习者更聪明。");
    addText(s, "面对抽象设计概念，先讲定义，还是先呈现反例？", 76, 210, 1080, 94,
      { fontSize: 32, bold: true, color: C.ink });
    addText(s, "A 班", 82, 344, 144, 46, { fontSize: 25, bold: true, color: C.accent });
    addText(s, "概念定义", 270, 338, 246, 62, { fontSize: 27, bold: true, color: C.ink });
    addText(s, "→", 540, 338, 80, 62, { fontSize: 32, color: C.accent });
    addText(s, "示例", 654, 338, 234, 62, { fontSize: 27, bold: true, color: C.ink });
    addRule(s, 80, 416, 1080, C.rule, 2);
    addText(s, "B 班", 82, 453, 144, 46, { fontSize: 25, bold: true, color: C.accent });
    addText(s, "反例", 270, 448, 160, 62, { fontSize: 27, bold: true, color: C.ink });
    addText(s, "→", 433, 448, 68, 62, { fontSize: 32, color: C.accent });
    addText(s, "概念定义", 516, 448, 210, 62, { fontSize: 27, bold: true, color: C.ink });
    addText(s, "→", 752, 448, 68, 62, { fontSize: 32, color: C.accent });
    addText(s, "简短练习", 830, 448, 230, 62, { fontSize: 27, bold: true, color: C.ink });
    addText(s, "问题是材料顺序，不是学生能力排名。", 82, 574, 1040, 42,
      { fontSize: 21, bold: true, color: C.caution });
    note(s, "原文问题：学生面对抽象设计概念时，概念定义、反例和交互练习应如何组合？A、B 班的材料顺序按原文保留。");
  }

  {
    const s = base(4, "方法：两班比较，但没有随机分组", "班级与教师因素可能不同；这一设计不支持因果归因。");
    addText(s, "48", 82, 220, 260, 112, { fontSize: 84, bold: true, color: C.accent });
    addText(s, "名学生 · 两班各 24 人", 88, 326, 460, 52,
      { fontSize: 27, bold: true, color: C.ink });
    addRect(s, 628, 216, 2, 328, { fill: C.rule });
    addText(s, "测量", 686, 222, 390, 38, { fontSize: 18, bold: true, color: C.accent });
    addText(s, "课前概念题 → 教学 → 课后概念题", 686, 277, 490, 78,
      { fontSize: 25, bold: true, color: C.ink });
    addText(s, "满分 100；仅提供两班组平均分。", 688, 377, 468, 38,
      { fontSize: 19, color: C.muted });
    addRect(s, 80, 500, 7, 84, { fill: C.caution });
    addText(s, "非随机分组 + 教师与班级基础可能不同", 108, 505, 1042, 35,
      { fontSize: 22, bold: true, color: C.caution });
    addText(s, "因此，后续数值只作描述性报告。", 108, 547, 1042, 34,
      { fontSize: 20, color: C.ink });
    note(s, "两班共 48 名学生，每班 24 人；未随机分组；概念题满分 100。未提供个体数据和方差。");
  }

  {
    const s = base(5, "证据边界：可以说与不能说", "原始材料没有个体数据和方差；统计推断与因果宣称均不成立。");
    addRect(s, 640, 218, 2, 360, { fill: C.rule });
    addText(s, "可以说", 84, 220, 450, 58, { fontSize: 32, bold: true, color: C.accent });
    ["课前 / 课后两班组平均分", "组平均变化：A +15，B +21", "两条匿名合成引语与课堂观察"].forEach((t, i) => {
      addText(s, `0${i + 1}`, 90, 310 + i * 82, 60, 40, { fontSize: 19, bold: true, color: C.accent });
      addText(s, t, 158, 306 + i * 82, 440, 58, { fontSize: 22, color: C.ink });
    });
    addText(s, "不能说", 690, 220, 430, 58, { fontSize: 32, bold: true, color: C.caution });
    ["统计显著性、置信区间", "哪种方法已被证明有效", "预算、时间或发表事实"].forEach((t, i) => {
      addText(s, t, 692, 310 + i * 82, 450, 62, { fontSize: 22, color: C.ink });
    });
    note(s, "没有个体数据和方差，不能计算显著性或置信区间。预算和时间未给出；材料是合成测试，不是真实发表。");
  }

  {
    const s = base(6, "描述性数据：两班均上升，差值并非因果效果", "原生可编辑表格；满分 100；变化为组均值之差。");
    const table = s.tables.add({
      rows: 3, columns: 5, left: 74, top: 245, width: 1110, height: 240,
      columnWidths: [390, 150, 190, 190, 190],
      values: [
        ["材料顺序", "人数", "课前平均", "课后平均", "平均变化"],
        ["A · 定义后示例", 24, 61, 76, "+15"],
        ["B · 反例后定义 + 练习", 24, 60, 81, "+21"],
      ],
    });
    styleTable(table, 3, 5, C, { fontSize: 20, headerFontSize: 18, headerFill: C.accent });
    addText(s, "61 / 60", 82, 534, 276, 58, { fontSize: 39, bold: true, color: C.accent });
    addText(s, "两班课前组平均几乎相同", 362, 542, 670, 45,
      { fontSize: 23, bold: true, color: C.ink });
    addText(s, "无个体数据与方差；未做显著性检验。", 82, 612, 900, 32,
      { fontSize: 17, color: C.caution });
    note(s, "表格数据全部来自原文：A 班 24 人，课前 61、课后 76、变化 +15；B 班 24 人，课前 60、课后 81、变化 +21。原文无个体数据和方差。");
  }

  {
    const s = base(7, "用柱形而非折线呈现组平均", "前后分数为描述性记录；图形不把组间差异伪装成实验效果。");
    addChart(s, "bar", {
      position: { left: 70, top: 215, width: 790, height: 404 },
      title: "课前与课后组平均分（满分 100）",
      categories: ["课前", "课后"],
      series: [
        { name: "A 班", values: [61, 76], fill: "#AABCB1" },
        { name: "B 班", values: [60, 81], fill: C.accent },
      ],
      barOptions: { direction: "column", grouping: "clustered", gapWidth: 75 },
      hasLegend: true,
      legend: { position: "bottom", overlay: false, textStyle: { fill: C.ink, fontSize: 16 } },
      xAxis: { line: { style: "solid", fill: C.rule, width: 1 }, textStyle: { fill: C.ink, fontSize: 16 } },
      yAxis: { min: 0, max: 100, majorUnit: 20, title: "组平均分", majorGridlines: { style: "solid", fill: C.rule, width: 1 }, textStyle: { fill: C.muted, fontSize: 14 } },
      dataLabels: { showValue: true, position: "outEnd", textStyle: { fill: C.ink, fontSize: 17, bold: true } },
      chartFill: C.paper, plotAreaFill: C.paper,
      chartLine: { fill: "none", width: 0 }, plotAreaLine: { fill: "none", width: 0 },
    });
    addRect(s, 900, 230, 5, 304, { fill: C.caution });
    addText(s, "请这样读", 934, 230, 258, 42, { fontSize: 21, bold: true, color: C.caution });
    addText(s, "两班课后分数都更高。\n\nB 班变化较大，仍只是描述性差异。\n\n不能据此断言材料顺序导致差异。", 934, 290, 256, 258,
      { fontSize: 20, color: C.ink });
    note(s, "柱形图的四个点为 A 61/76、B 60/81；刻意不用两组折线图。未作因果归因。");
  }

  {
    const s = base(8, "两条引语指向不同的学习阻力", "匿名合成访谈摘录；保留原意，不把个体感受当作总体证明。");
    addText(s, "“", 70, 188, 120, 110, { fontSize: 82, color: C.accent });
    addText(s, "先看哪里不对，比较容易理解为什么要这样做。", 160, 226, 960, 82,
      { fontSize: 29, bold: true, color: C.ink });
    addText(s, "学生甲 · 匿名合成引语", 164, 324, 570, 35, { fontSize: 16, color: C.muted });
    addRule(s, 164, 394, 940, C.rule, 2);
    addText(s, "“", 70, 417, 120, 110, { fontSize: 82, color: C.caution });
    addText(s, "例子太多时，反而忘记定义。", 160, 455, 880, 82,
      { fontSize: 29, bold: true, color: C.ink });
    addText(s, "学生乙 · 匿名合成引语", 164, 554, 570, 35, { fontSize: 16, color: C.muted });
    note(s, "两条匿名引语逐字来自 D04 原始材料；均为合成访谈摘录，不代表总体意见。");
  }

  {
    const s = base(9, "课堂观察：让变化可见，也要管住时间", "图示与练习服务概念理解，不能替代概念定义。");
    row(s, 219, "01", "标出变化对象", "不只用颜色；位置、标签与形状一起说明‘什么变了’。 ");
    row(s, 341, "02", "前后对照", "展示前后变化比反复讲定义更容易引发讨论。 ");
    row(s, 463, "03", "控制练习时间", "简短练习需要边界，避免挤占解释与讨论。 ");
    note(s, "观察来自合成材料。原文只说需要控制练习时间，未提供具体分钟数；本页未编造。");
  }

  {
    const s = base(10, "结论：看见差异，提出待检验流程", "本页结论与下一页局限同等分量；描述性差异不等于已证实效果。");
    addText(s, "我们看见的", 80, 236, 476, 43, { fontSize: 19, bold: true, color: C.accent });
    addText(s, "A 班 61 → 76  ·  +15\nB 班 60 → 81  ·  +21", 80, 307, 520, 145,
      { fontSize: 32, bold: true, color: C.ink });
    addText(s, "两班课后组平均均上升；B 班变化较大。", 82, 493, 485, 92,
      { fontSize: 20, color: C.muted });
    addRect(s, 634, 228, 2, 346, { fill: C.rule });
    addText(s, "我们提出的", 690, 236, 466, 43, { fontSize: 19, bold: true, color: C.accent });
    addText(s, "反例先行\n→ 概念定义\n→ 简短练习", 690, 314, 458, 225,
      { fontSize: 36, bold: true, color: C.ink });
    addText(s, "这是可供后续研究检验的流程，尚非被证明的方法。", 80, 611, 1075, 36,
      { fontSize: 19, bold: true, color: C.caution });
    note(s, "贡献主张是提出可供后续研究检验的材料组织流程；不能宣称该方法已被证明有效。");
  }

  {
    const s = base(11, "局限：这份证据不能证明效果", "与结论同等醒目；以下每一项都限制对差异的解释。");
    const items = [
      ["样本小", "只有 48 名学生、两个班。"],
      ["非随机", "班级不是随机分配。"],
      ["教师因素", "教师与班级基础可能不同。"],
      ["只测短期", "没有延迟测验与过程表现。"],
    ];
    items.forEach(([heading, detail], i) => {
      const col = i % 2; const rowIndex = Math.floor(i / 2);
      const x = col ? 660 : 80; const y = rowIndex ? 448 : 234;
      addText(s, heading, x, y, 480, 62, { fontSize: 35, bold: true, color: C.caution });
      addText(s, detail, x, y + 83, 475, 64, { fontSize: 21, color: C.ink });
    });
    addRect(s, 627, 225, 2, 378, { fill: C.rule });
    addRule(s, 80, 415, 1110, C.rule, 2);
    note(s, "四项局限逐项对应原文：样本小、班级非随机、教师因素、只测短期表现。不能因果归因或计算显著性。");
  }

  {
    const s = base(12, "下一步：让假说真正可检验", "先补设计，再评估材料顺序；预算与时间仍待确定。");
    row(s, 220, "01", "随机或交叉设计", "降低班级基础与教师因素对比较的干扰。 ");
    row(s, 342, "02", "记录延迟测验", "不只看课后即时概念题，还要观察保持情况。 ");
    row(s, 464, "03", "记录过程表现", "把课堂练习与讨论过程纳入后续研究。 ");
    addText(s, "预算与时间：原始材料未给出，不作估算。", 174, 608, 900, 34,
      { fontSize: 18, bold: true, color: C.caution });
    note(s, "下一步按原文：随机或交叉设计，记录延迟测验与过程表现。预算和时间未给出。");
  }

  return {
    id: "D04",
    title: "交互式课堂提示研究答辩",
    presentation: p,
    requirements: {
      explicitTotalSlideCount: 12,
      requiredNativeTableOwnerSlides: [6],
      requiredNativeChartOwnerSlides: [7],
      materializeLiteralChartWorkbooks: true,
    },
  };
}

function buildD05() {
  const p = presentation();
  const C = {
    bg: "#F2F1EB", paper: "#FFFEFA", ink: "#1F292B", muted: "#536062",
    accent: "#204B9B", blueSoft: "#E4ECFA", amber: "#A86613",
    amberSoft: "#F8EBD6", green: "#286451", red: "#A54736",
    rule: "#D4D9D4", secondary: "#B9C2C0",
  };
  const source = "SRC-T01 · 合成教学材料 · 非外部实证";
  const base = (number, title, subtitle = "") => {
    const s = p.slides.add();
    s.background.fill = number % 2 ? C.paper : C.bg;
    sectionTitle(s, number, title, subtitle, C);
    footer(s, number, source, C.muted);
    return s;
  };
  const note = (s, detail) => notes(s, `Source: D05_teaching_notes.md (${source})。${detail}`);
  const chip = (s, x, y, width, label, fill, ink = C.ink) => {
    addRect(s, x, y, width, 44, { fill });
    addText(s, label, x + 16, y + 7, width - 32, 30, { fontSize: 17, bold: true, color: ink });
  };

  {
    const s = p.slides.add();
    s.background.fill = C.ink;
    addRect(s, 0, 0, 18, H, { fill: C.accent });
    addText(s, "设计基础课 / 8 页学习材料", 76, 68, 650, 34,
      { fontSize: 17, bold: true, color: "#ADC5EF" });
    addText(s, "颜色不是装饰，\n它要给信息分工", 76, 159, 870, 178,
      { fontSize: 55, bold: true, color: "#FFFFFF" });
    addText(s, "建立主次 · 区分类别 · 表示状态", 80, 386, 780, 44,
      { fontSize: 26, color: "#E3E9EA" });
    addRule(s, 80, 495, 1068, "#708184", 2);
    addText(s, "看一张混乱版面 → 改进 → 自己排四条信息", 80, 527, 980, 76,
      { fontSize: 23, color: "#E3E9EA" });
    addText(s, "SRC-T01 · 合成教学案例，非外部实证结论", 80, 659, 720, 24,
      { fontSize: 14, color: "#ADC5EF" });
    note(s, "课程目标是理解色彩在信息版面中的三种任务；不教‘某色永远对应某情绪’。");
  }

  {
    const s = base(2, "今天只学三种任务", "每种用法都服务理解；不存在“某色永远对应某种情绪”。");
    const parts = [
      { n: "01", title: "建立主次", body: "谁先被看见？", y: 223, c: C.accent },
      { n: "02", title: "区分类别", body: "哪些属于同一类？", y: 359, c: C.amber },
      { n: "03", title: "表示状态", body: "现在需要什么动作？", y: 495, c: C.green },
    ];
    parts.forEach(({ n, title, body, y, c }) => {
      addRect(s, 78, y + 9, 14, 77, { fill: c });
      addText(s, n, 119, y + 2, 92, 69, { fontSize: 38, bold: true, color: c });
      addText(s, title, 259, y, 340, 58, { fontSize: 34, bold: true, color: C.ink });
      addText(s, body, 692, y + 7, 445, 44, { fontSize: 24, color: C.muted });
      addRule(s, 259, y + 93, 888, C.rule, 1);
    });
    note(s, "三种任务按原始材料顺序：主次、类别、状态；课程不是颜色—情绪速查表。");
  }

  {
    const s = base(3, "先找问题：你的第一眼看哪里？", "三组示意数字同样亮、同样大；本页数字仅供练习，不代表真实资料。");
    const values = ["24", "12", "6"];
    const labels = ["主要结论", "支撑 A", "支撑 B"];
    values.forEach((value, i) => {
      const x = 79 + i * 391;
      addRect(s, x, 263, 349, 245, { fill: "#E6E8E5" });
      addText(s, value, x + 29, 300, 290, 110,
        { fontSize: 72, bold: true, color: "#697270", alignment: "center" });
      addText(s, labels[i], x + 29, 426, 290, 40,
        { fontSize: 22, color: "#697270", alignment: "center" });
    });
    addText(s, "先别翻页：如果只看 2 秒，哪个数字是主角？为什么？", 82, 560, 1080, 62,
      { fontSize: 25, bold: true, color: C.accent });
    note(s, "教学安排第一步：给混乱例子，请学生找视觉焦点。24/12/6 是本练习的虚构示意计数，下一页保持数值含义完全不变。");
  }

  {
    const s = base(4, "改进：只突出真正的主数字", "数字与含义不变；改变的是对比、面积与辅助色。");
    addRect(s, 76, 235, 612, 333, { fill: C.blueSoft });
    addRect(s, 76, 235, 10, 333, { fill: C.accent });
    addText(s, "24", 119, 272, 500, 155, { fontSize: 106, bold: true, color: C.accent });
    addText(s, "主要结论", 127, 445, 470, 58, { fontSize: 30, bold: true, color: C.ink });
    addText(s, "12", 770, 260, 212, 92, { fontSize: 56, bold: true, color: C.muted });
    addText(s, "支撑 A", 774, 349, 238, 44, { fontSize: 22, color: C.muted });
    addRule(s, 772, 426, 386, C.rule, 2);
    addText(s, "6", 770, 453, 212, 92, { fontSize: 56, bold: true, color: C.muted });
    addText(s, "支撑 B", 774, 541, 238, 44, { fontSize: 22, color: C.muted });
    addText(s, "主次来自可见的视觉差；不是篡改数据。", 80, 615, 1030, 36,
      { fontSize: 20, bold: true, color: C.accent });
    note(s, "教学安排第二步：展示改进。三项示意数字仍为 24、12、6，标签仍分别为主要结论、支撑 A、支撑 B；只调整视觉权重。");
  }

  {
    const s = base(5, "类别：同类对象共享语义", "类别一多，就加标签、位置或形状；不要让颜色独自承担区别。");
    addText(s, "类别 A", 83, 225, 296, 46, { fontSize: 25, bold: true, color: C.accent });
    addText(s, "类别 B", 680, 225, 296, 46, { fontSize: 25, bold: true, color: C.amber });
    [294, 390, 486].forEach((y, i) => {
      addCircle(s, 87, y, 54, C.accent);
      addText(s, `A${i + 1} · 同类信息`, 164, y + 5, 391, 54,
        { fontSize: 23, bold: true, color: C.ink });
      addRect(s, 682, y + 2, 54, 54, { fill: C.amber });
      addText(s, `B${i + 1} · 同类信息`, 764, y + 5, 390, 54,
        { fontSize: 23, bold: true, color: C.ink });
    });
    addRect(s, 80, 607, 1120, 4, { fill: C.rule });
    addText(s, "同一类别不应每页任意换色；这里还用圆/方、A/B 与位置作冗余编码。",
      82, 623, 1120, 38, { fontSize: 18, color: C.muted });
    note(s, "例子二：同类对象共享色彩语义，不要每页任意换一套；类别很多时增加标签、位置或形状。这些 A/B 项是抽象图解，不代表真实数据。");
  }

  {
    const s = base(6, "状态：不能只留绿黄红三个点", "文字与符号负责说明动作，色彩只作为额外线索。");
    const states = [
      { y: 228, color: C.green, symbol: "✓", title: "正常", action: "继续当前流程" },
      { y: 367, color: C.amber, symbol: "!", title: "注意", action: "检查信息再继续" },
      { y: 506, color: C.red, symbol: "×", title: "错误", action: "停下并修正" },
    ];
    states.forEach(({ y, color, symbol, title, action }) => {
      addRect(s, 81, y, 1078, 108, { fill: C.paper, line: { style: "solid", fill: C.rule, width: 1 } });
      addCircle(s, 105, y + 24, 59, color);
      addText(s, symbol, 116, y + 24, 39, 57,
        { fontSize: 37, bold: true, color: "#FFFFFF", alignment: "center" });
      addText(s, title, 212, y + 27, 226, 58, { fontSize: 32, bold: true, color: C.ink });
      addText(s, action, 561, y + 31, 475, 54, { fontSize: 24, color: C.muted });
    });
    note(s, "例子三：正常/注意/错误不能只有绿黄红点，须同时有图标或文字。不同文化、屏幕和视觉差异会影响理解，应在具体使用场景验证。");
  }

  {
    const s = base(7, "轮到你：把四条信息排成层级", "请标出一个主要结论、两个支撑、一个备注；不需要真实公司资料。");
    const items = [
      ["甲", "去掉颜色后，信息也应有清楚的阅读顺序。"],
      ["乙", "主数字可以增加明度对比或面积。"],
      ["丙", "同类对象共享标签、形状与色彩语义。"],
      ["丁", "这些是教学示例，不代表真实公司资料。"],
    ];
    items.forEach(([marker, body], i) => {
      const y = 222 + i * 101;
      addText(s, marker, 83, y + 2, 70, 55, { fontSize: 31, bold: true, color: C.accent });
      addText(s, body, 178, y, 1000, 74, { fontSize: 24, color: C.ink });
      addRule(s, 178, y + 76, 944, C.rule, 1);
    });
    addText(s, "先说出你想让谁先被看见，再动手改字号、位置和颜色。", 82, 642, 1050, 34,
      { fontSize: 18, bold: true, color: C.accent });
    note(s, "教学安排第三步：学生把四条信息分为一个主要结论、两个支撑、一个备注。此页四条都是无真实公司资料的练习文本。");
  }

  {
    const s = base(8, "最后一问：去掉颜色，还看得懂吗？", "先用结构建立层级，再用颜色强化；这是可投屏、可课后复读的自检法。");
    addRect(s, 76, 224, 12, 354, { fill: C.ink });
    addText(s, "主要结论", 118, 229, 330, 44, { fontSize: 18, bold: true, color: C.muted });
    addText(s, "去掉颜色后，\n仍要读得懂", 115, 294, 635, 143,
      { fontSize: 49, bold: true, color: C.ink });
    addRule(s, 796, 241, 2, C.rule, 2);
    addText(s, "支撑 01", 842, 246, 300, 31, { fontSize: 17, bold: true, color: C.muted });
    addText(s, "明度 / 面积建立主次", 842, 292, 337, 75,
      { fontSize: 24, bold: true, color: C.ink });
    addText(s, "支撑 02", 842, 405, 300, 31, { fontSize: 17, bold: true, color: C.muted });
    addText(s, "标签 / 形状辅助类别与状态", 842, 450, 337, 98,
      { fontSize: 24, bold: true, color: C.ink });
    addText(s, "备注：文化、屏幕与视觉差异会改变读法；在真实使用场景再验证。",
      115, 597, 1044, 53, { fontSize: 18, color: C.muted });
    note(s, "保留原始材料的结尾提问：‘如果去掉颜色，这张图还看得懂吗？’。本页示范一个主要结论、两个支撑与一个备注；实际课堂可接受其他有理有据的分法。");
  }

  return {
    id: "D05",
    title: "用色彩建立信息层级",
    presentation: p,
    requirements: {
      explicitTotalSlideCount: 8,
      requiredNativeTableOwnerSlides: [],
      requiredNativeChartOwnerSlides: [],
    },
  };
}

async function assertNew(pathname) {
  try {
    await fs.access(pathname);
  } catch {
    return;
  }
  throw new Error(`Refusing to overwrite existing artifact: ${pathname}`);
}

async function runOpcContentTypesGuard(args) {
  const { stdout } = await execFileAsync(
    runtimePython,
    [path.join(workspaceDir, "src/lantai/ooxml_content_types.py"), ...args],
    {
      cwd: workspaceDir,
      env: process.env,
      maxBuffer: 1024 * 1024,
    },
  );
  return JSON.parse(stdout.trim());
}

async function finalizeDeck(deck) {
  const started = nowMs();
  const deckSlug = deck.id.toLowerCase();
  const stagingDir = path.join(STAGING_ROOT, deckSlug);
  await fs.mkdir(stagingDir, { recursive: true });
  await fs.mkdir(OUTPUT_DIR, { recursive: true });

  const candidatePath = path.join(stagingDir, `${deckSlug}-${revision}-candidate.pptx`);
  const preparedCandidatePath = path.join(stagingDir, `${deckSlug}-${revision}-opc-prepared-candidate.pptx`);
  const draftPath = path.join(OUTPUT_DIR, `${deckSlug}-${revision}-first-draft.pptx`);
  const finalPath = path.join(OUTPUT_DIR, `${deckSlug}-${revision}-final.pptx`);
  const receiptPath = path.join(stagingDir, `${deckSlug}-${revision}-validation.json`);
  for (const pathname of [candidatePath, preparedCandidatePath, draftPath, finalPath, receiptPath]) await assertNew(pathname);

  const exportStarted = nowMs();
  await (await PresentationFile.exportPptx(deck.presentation)).save(candidatePath);
  await fs.copyFile(candidatePath, draftPath);
  const exportMs = nowMs() - exportStarted;

  const opcPrepareStarted = nowMs();
  const preparedOpcContentTypes = await runOpcContentTypesGuard([
    "prepare-literal-chart-candidate",
    "--source", candidatePath,
    "--output", preparedCandidatePath,
  ]);
  const opcPrepareMs = nowMs() - opcPrepareStarted;

  const validationStarted = nowMs();
  const result = await finalizePresentation({
    ...deck.requirements,
    workspaceDir,
    candidatePath: preparedCandidatePath,
    finalPath,
    pythonExecutable: runtimePython,
    integrityValidatorPath: path.join(skillDir, "container_tools/inspect_presentation_package_integrity.py"),
    layoutValidatorPath: path.join(skillDir, "container_tools/inspect_presentation_layout_geometry.py"),
    layoutArgs: [
      "--expected-slide-size-emu", EXPECTED_SLIDE_SIZE_EMU,
      "--validate-bullet-geometry",
      "--validate-heading-fit",
      ...deck.requirements.requiredNativeTableOwnerSlides.flatMap((number) => ["--require-native-table-slide", String(number)]),
    ],
    requiredNativeTableOwnerSlides: deck.requirements.requiredNativeTableOwnerSlides,
    requiredNativeChartOwnerSlides: deck.requirements.requiredNativeChartOwnerSlides,
    fontPolicy: { basis: "design", families: [FONT], scriptFonts: { ea: FONT } },
    verifyArtifactToolImport: true,
    receiptPath,
  });
  const finalizedOpcContentTypes = await runOpcContentTypesGuard([
    "check",
    "--input", finalPath,
    "--require-literal-chart-workbook",
  ]);
  const validationMs = nowMs() - validationStarted;

  return {
    id: deck.id,
    title: deck.title,
    revision,
    draftPath: path.relative(workspaceDir, draftPath),
    preparedCandidatePath: path.relative(workspaceDir, preparedCandidatePath),
    finalPath: path.relative(workspaceDir, finalPath),
    receiptPath: path.relative(workspaceDir, receiptPath),
    slideCount: deck.presentation.slides.items.length,
    requiredNativeTableOwnerSlides: deck.requirements.requiredNativeTableOwnerSlides,
    requiredNativeChartOwnerSlides: deck.requirements.requiredNativeChartOwnerSlides,
    timingsMs: {
      exportDraft: Math.round(exportMs),
      prepareOpcCandidate: Math.round(opcPrepareMs),
      validateAndFinalize: Math.round(validationMs),
      total: Math.round(nowMs() - started),
    },
    opcContentTypes: {
      preparedCandidate: preparedOpcContentTypes,
      finalizedPackage: finalizedOpcContentTypes,
    },
    finalizer: result,
  };
}

await fs.mkdir(OUTPUT_DIR, { recursive: true });
await fs.mkdir(STAGING_ROOT, { recursive: true });

const builders = { D01: buildD01, D02: buildD02, D03: buildD03, D04: buildD04, D05: buildD05 };
const requestedCases = (process.env.DECK_CASES ?? Object.keys(builders).join(","))
  .split(",").map((id) => id.trim()).filter(Boolean);
if (requestedCases.some((id) => !(id in builders)) || new Set(requestedCases).size !== requestedCases.length) {
  throw new Error(`Unknown or duplicate DECK_CASES: ${requestedCases.join(",")}`);
}
const results = [];
for (const id of requestedCases) {
  const build = builders[id];
  results.push(await finalizeDeck(build()));
}

const manifestPath = path.join(OUTPUT_DIR, `development-decks-${revision}-manifest.json`);
await assertNew(manifestPath);
await fs.writeFile(manifestPath, `${JSON.stringify({
  generatedAt: new Date().toISOString(),
  sourceKind: "synthetic-development-materials",
  authoringStrategy: "page-level-native-artifact-tool",
  fontPolicy: { basis: "design", families: [FONT], scriptFonts: { ea: FONT } },
  results,
}, null, 2)}\n`);

console.log(JSON.stringify({ manifestPath, results }, null, 2));
