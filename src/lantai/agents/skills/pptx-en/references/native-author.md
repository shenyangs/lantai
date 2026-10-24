# Per-slide native author

Order `visual_direction.palette` as canvas, primary text, and optional accent. When supplied, the autonomous design context consumes the author's visual concept and role colors; it must not mistake a built-in synthetic style for an external reference. HARD brand constraints and explicit user color take precedence. Other direction fields retain author intent, but fonts, imagery, and motifs still require inspection of the final pages.

`create_presentation` accepts the complete slide/object schema in one call. For new work, supply the complete `brief`, `source_paths`, `required_facts`, `immutable_data`, `visual_direction`, and paired `narrative_arc`/`storyboard`; `title` is not the brief. The storyboard matches the final slide count, page-level `source_indexes` reference `source_paths` from zero, and every input material must map to at least one page. This checks assignment, not extraction completeness. Page-level required/immutable facts must come from the deck-level sets. Historical calls may omit the storyboard for compatibility.

The slide author consumes an edited storyboard and a unified visual direction. Prefer native text, shape, image, table, and chart objects while retaining semantic roles, provenance, and immutable data. The `deck-plan.json` checkpoint binds page claims to native slide IDs for the per-slide critic, while the narrative arc reaches its deck context. This tool still submits all slides at once; it does not yet provide page-level generation or recovery.

Set `composition_authority: "author"` if and only if every object on a freeform slide has an explicit frame. That marks coordinates and z-order as authored intent; downstream code may measure, constrain, serialize, and validate but must not flatten or relayout the scene. Use `design_system` when frames are incomplete.

Never insert a meaningless Shape to make a page appear authored. Tables, charts, text-only pages, and image pages may all be authoritative. When real measurement finds overflow or unreadability, return the page to the author for a local repair instead of silently replacing its layout downstream.
