export interface WorkMethod {
  id: string;
  name: string;
  summary: string;
  instructions: string[];
  scope: string[];
  acceptance: string[];
  /** @deprecated Accepted only when reading legacy custom methods. */
  max_steps?: number | null;
  /** @deprecated Accepted only when reading legacy custom methods. */
  max_tool_calls?: number | null;
  /** @deprecated Accepted only when reading legacy custom methods. */
  max_wall_seconds?: number | null;
  max_no_progress_iterations: number;
  built_in: boolean;
}

export type WorkMethodDraft = Omit<WorkMethod, "id" | "built_in">;
