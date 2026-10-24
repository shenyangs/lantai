import type { AgentToolCall, AgentToolDef, ToolExecution } from './types'

/**
 * A skill packages one capability domain for the agent loop: its system
 * prompt section, its tools, per-turn context, and the tool executor.
 * AI Docs ships a docx skill; Excel / PPT skills plug in the same way.
 */
export interface AgentSkill {
  id: string
  /** system prompt section describing this skill's rules and tools */
  systemPrompt: string
  tools: AgentToolDef[]
  /**
   * Fresh context sections attached to every user turn (e.g. document
   * skeleton + selection). Return '' when there is nothing to attach.
   */
  buildContext?(): string
  /**
   * signal: aborted when the user hits stop. Long-running tools (e.g.
   * generate_deck with internal LLM calls) should check signal.aborted in
   * their loops and stop promptly.
   */
  executeTool(call: AgentToolCall, signal?: AbortSignal): ToolExecution | Promise<ToolExecution>
}

/**
 * Fail-closed capability registration. Only schema-valid, uniquely named tools are advertised,
 * and the executor wrapper refuses any model-emitted tool that was not on that exact surface.
 * This is the shared authenticity gate used by every Office editor.
 */
export function verifyAgentSkill(skill: AgentSkill): AgentSkill {
  const names = new Set<string>()
  for (const tool of skill.tools) {
    const name = String(tool.name ?? '').trim()
    if (!name) throw new Error(`skill ${skill.id}: tool name must not be empty`)
    if (names.has(name)) throw new Error(`skill ${skill.id}: duplicate tool name: ${name}`)
    if (!tool.inputSchema || tool.inputSchema.type !== 'object') {
      throw new Error(`skill ${skill.id}: ${name} must declare an object input schema`)
    }
    const properties = tool.inputSchema.properties
    if (!properties || typeof properties !== 'object' || Array.isArray(properties)) {
      throw new Error(`skill ${skill.id}: ${name} must declare schema properties`)
    }
    const required = tool.inputSchema.required
    if (
      Array.isArray(required) &&
      required.some((field) => typeof field !== 'string' || !(field in properties))
    ) {
      throw new Error(`skill ${skill.id}: ${name} has an undeclared required field`)
    }
    names.add(name)
  }
  return {
    ...skill,
    tools: [...skill.tools],
    executeTool: (call, signal) => {
      if (!names.has(call.name)) {
        return {
          output: `Capability not registered for this editor: ${call.name}`,
          isError: true,
          summary: call.name,
        }
      }
      return skill.executeTool(call, signal)
    },
  }
}

/**
 * Merge several skills into one (tool names must be globally unique).
 * `intro` becomes the shared preamble of the combined system prompt.
 */
export function composeSkills(id: string, intro: string, skills: AgentSkill[]): AgentSkill {
  const owner = new Map<string, AgentSkill>()
  for (const skill of skills) {
    for (const tool of skill.tools) {
      if (owner.has(tool.name)) throw new Error(`duplicate tool name: ${tool.name}`)
      owner.set(tool.name, skill)
    }
  }
  return verifyAgentSkill({
    id,
    systemPrompt: [intro, ...skills.map((s) => s.systemPrompt)].filter(Boolean).join('\n\n'),
    tools: skills.flatMap((s) => s.tools),
    buildContext: () =>
      skills
        .map((s) => s.buildContext?.() ?? '')
        .filter(Boolean)
        .join('\n\n'),
    executeTool: (call, signal) => {
      const skill = owner.get(call.name)
      if (!skill) {
        return { output: `Unknown tool: ${call.name}`, isError: true, summary: call.name }
      }
      return skill.executeTool(call, signal)
    },
  })
}
