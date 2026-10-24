# Tool ownership

Runtime owns orchestration, permissions, workspace, and delivery. Presentation Skill owns content, narrative, design, and tool choice. DeckIR owns stable identity, facts, provenance, and version checkpoints. PptxGenJS composes new native PPTX files. python-pptx inspects and performs limited edits. OfficeCLI queries, validates, and runs bounded batch edits. GenOffice previews, saves, and hosts human editing. One mutation has exactly one writer.

`lantai.presentation.render`, `lantai.presentation.critic`, and the quality gate centrally render and review the final PPTX. The Skill does not bundle a second thumbnail path or a direct JavaScript writer.

## Product tools

| Tool | Responsibility | Forbidden implicit behavior |
| --- | --- | --- |
| `create_presentation` | Create the only durable PPT job and return its `job_id` | Do not use `title` as the complete brief or create another job after a foreground timeout |
| `get_presentation_generation_status` | Read stage, budget, quality, and publish readiness | Do not copy, send, or finalize |
| `retry_presentation_generation` | Atomically increment the bounded retry count and resume the original job/checkpoints only when every route permits automatic recovery | Do not create a job, reset budget, treat pause/Stop as failure retry, or externally publish implicitly |
| `finalize_presentation_generation` | Idempotently publish an artifact with bound passing evidence | Do not bypass quality or version checks |
| `resolve_pending_deliverable_choice` | Atomically resolve continue / replace / cancel within the original Task | Do not infer intent from global numbers, regular expressions, or keywords |

## Lifecycle

Native remains the only Task owner. A PPT job records stages, checkpoints, cost, and recovery state; it is not a second task executor. Disconnect, foreground wait timeout, and user Stop are distinct. The first two do not automatically cancel a durable job. Stop must halt later composition and critique at a safe per-slide boundary and must never publish.

Failed-job retry requires complete persisted routes on the latest failure event, every route must permit automatic recovery, and a hard attempt limit applies. It resumes the original `job_id` and passing checkpoints without resetting cost. Changes to shared resources, immutable data, storyboard, or visual direction must propagate dirty dependencies correctly. Every utility-model, image, and critic expense is charged to the existing Runtime budget; the Skill must not create a side account or bypass authorization.
