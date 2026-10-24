"""Bounded native text edits; an edit never invents caption provenance."""

from .models import RichTextRun, TextObject


def _replace_unique_inline_run(obj: TextObject, text: str) -> list[RichTextRun] | None:
    """Reuse styles only when exact unchanged neighbors locate one carrier.

    This does not infer formatting for a whole-body rewrite, boundary insertion,
    or a replacement spanning runs. Metric/caption provenance remains governed
    by the separate complete value/label/detail contract below.
    """
    runs = obj.runs
    if (
        obj.role == "metric" or obj.native_metric_text is not None
        or sum(bool(run.text) and not run.paragraph_break for run in runs) < 2
        or "".join(run.text for run in runs) != obj.text
        or any(
            run.text not in {"\n", "\v"} if run.paragraph_break
            else "\n" in run.text or "\v" in run.text
            for run in runs
        )
    ):
        return None
    carriers: list[tuple[int, str]] = []
    for index, run in enumerate(runs):
        if run.paragraph_break or not run.text:
            continue
        prefix = "".join(item.text for item in runs[:index])
        suffix = "".join(item.text for item in runs[index + 1:])
        if (
            not text.startswith(prefix) or not text.endswith(suffix)
            or len(text) <= len(prefix) + len(suffix)
        ):
            continue
        replacement = text[len(prefix):len(text) - len(suffix) if suffix else None]
        if "\n" not in replacement and "\v" not in replacement:
            carriers.append((index, replacement))
    if len(carriers) != 1:
        return None
    index, replacement = carriers[0]
    result = list(runs)
    result[index] = result[index].model_copy(update={"text": replacement})
    return result


def replace_native_text_object(obj: TextObject, text: str) -> TextObject:
    """Retain styles for a bounded inline edit or known metric label edit.

    Whole-body replacements keep the existing unformatted replacement behavior.
    Only an unambiguous, complete value/label/detail structure may reuse a
    caption identity. A changed label paragraph must have one native run;
    unsupported mixed formatting is not guessed or flattened into a caption.
    """
    if text == obj.text:
        return obj
    fallback = obj.model_copy(update={
        "text": text, "runs": [], "native_metric_text": None,
    })
    inline_runs = _replace_unique_inline_run(obj, text)
    if inline_runs is not None:
        return obj.model_copy(update={"text": text, "runs": inline_runs})
    metric = obj.native_metric_text
    if obj.role != "metric" or metric is None or not obj.runs:
        return fallback
    before, after = obj.text.split("\n"), text.split("\n")
    if (
        len(before) != len(after)
        or not metric.caption_paragraphs(before)
        or "".join(run.text for run in obj.runs) != obj.text
    ):
        return fallback
    value, label, detail = (
        part.split("\n") for part in (metric.value, metric.label, metric.detail)
    )
    layouts = []
    if before == value + label + detail:
        layouts.append(range(len(value), len(value) + len(label)))
    if before == label + value + detail:
        layouts.append(range(len(label)))
    if len(layouts) != 1:
        return fallback
    label_indices = layouts[0]
    if (
        any(
            old != new for index, (old, new) in enumerate(zip(before, after))
            if index not in label_indices
        )
        or any(not after[index].strip() for index in label_indices)
    ):
        return fallback
    paragraphs: list[list[int]] = [[]]
    for index, run in enumerate(obj.runs):
        if run.paragraph_break:
            if run.text != "\n":
                return fallback
            paragraphs.append([])
        else:
            if "\n" in run.text or "\v" in run.text:
                return fallback
            paragraphs[-1].append(index)
    if len(paragraphs) != len(before):
        return fallback
    runs = list(obj.runs)
    for index in label_indices:
        if before[index] == after[index]:
            continue
        carriers = [item for item in paragraphs[index] if runs[item].text]
        if len(carriers) != 1:
            return fallback
        carrier = carriers[0]
        runs[carrier] = runs[carrier].model_copy(update={"text": after[index]})
    edited_metric = metric.model_copy(update={
        "label": "\n".join(after[index] for index in label_indices),
    })
    return obj.model_copy(update={
        "text": text, "runs": runs, "native_metric_text": edited_metric,
    })
