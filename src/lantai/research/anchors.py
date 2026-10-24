"""Exact text anchoring only; never makes a semantic evidence judgment."""
def resolve_span(text, value, *, text_key='text', base=0):
    a, b = value.get('start'), value.get('end')
    anchor = value.get(text_key)
    if isinstance(a, int) and not isinstance(a, bool) and isinstance(b, int) and not isinstance(b, bool):
        if not base <= a < b <= base + len(text):
            raise ValueError('Exact quote/anchor offsets are outside the supplied text')
        if anchor is not None and text[a - base:b - base] != anchor:
            raise ValueError('Exact quote/anchor does not match its offsets')
        return a, b
    if a is not None or b is not None:
        raise ValueError('Supply both offsets or an exact text anchor')
    if not isinstance(anchor, str) or not anchor:
        raise ValueError('A nonempty exact text quote/anchor is required')
    position = text.find(anchor)
    if position < 0:
        raise ValueError('Exact quote/anchor is absent from the supplied version')
    if text.find(anchor, position + 1) >= 0:
        raise ValueError('Exact quote/anchor is ambiguous; supply verified offsets')
    return base + position, base + position + len(anchor)
