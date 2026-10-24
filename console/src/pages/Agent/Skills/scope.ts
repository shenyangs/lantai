export function describeSkillChannelScope(channels?: string[]) {
  const values = channels?.length ? channels : ["all"];
  if (values.includes("all")) return "可用于所有消息渠道";
  return `适用于 ${values.join("、")} 渠道`;
}
