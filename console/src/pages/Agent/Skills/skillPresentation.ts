const KNOWN_SKILL_TITLES: Record<string, string> = {
  americano: "策略与决策",
  barley_tea: "经营与供应链",
  black_coffee: "财务与会计",
  black_tea: "销售与客户",
  clear_tea: "新闻与编辑",
  cocoa: "人才与组织",
  coconut: "研究与证据",
  cola: "创意文案",
  cold_brew: "科学与工程",
  latte: "办公与文档",
  longjing: "复核与质量",
  matcha: "写作与编辑",
  milk_tea: "市场与增长",
  mocha: "产品与体验",
  orange: "视觉与创意",
  puer: "投资研究",
  rock_tea: "法律与合规",
  soda: "数据与量化",
  sprite: "媒体与传播",
  warm_water: "健康信息",
  white_tea: "政策与公共事务",
  ai_taste_rewrite: "真人感改写",
  "ai-taste-rewrite": "真人感改写",
  "app-icon-designer": "应用图标设计",
  "browser-testing": "网页体验验收",
  "agent-designer": "智能体设计",
  "agent-run-doctor": "智能体运行诊断",
};

function normalizedKey(value: string) {
  return value.trim().toLocaleLowerCase().replace(/-/g, "_");
}

export function getSkillDisplayName(name: string, description = "") {
  const known =
    KNOWN_SKILL_TITLES[name.toLocaleLowerCase()] ||
    KNOWN_SKILL_TITLES[normalizedKey(name)];
  if (known) return known;

  const normalizedDescription = description.replace(/\s+/g, " ").trim();
  if (/[\u3400-\u9fff]/.test(normalizedDescription)) {
    const first = normalizedDescription
      .replace(/^(?:当任务需要|用于|负责|通过)/, "")
      .split(/[；。—]/, 1)[0]
      ?.trim();
    if (first && first.length <= 28) return first;
  }

  return name
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (letter) => letter.toLocaleUpperCase());
}

export function getSkillSlugLabel(name: string) {
  return `技术名称：${name}`;
}
