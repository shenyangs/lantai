"""Manual regions refer to rendered PDF pixels, never extracted text coordinates."""
import math
from .pdf_preview import preview_page


def validate_region(library, db, work_id, anchor):
    keys = {'source_version_id', 'source_hash', 'page', 'coordinate_system', 'x', 'y', 'width', 'height'}
    if not isinstance(anchor, dict) or set(anchor) != keys:
        raise ValueError('Invalid PDF region anchor')
    if anchor['coordinate_system'] != 'pdf_raster_normalized_top_left_v1':
        raise ValueError('Invalid PDF region coordinate system')
    if type(anchor['page']) is not int or anchor['page'] < 1:
        raise ValueError('Invalid PDF region page')
    for key in ('x', 'y', 'width', 'height'):
        value = anchor[key]
        if type(value) not in (int, float) or not 0 <= value <= 1 or not math.isfinite(value):
            raise ValueError('Invalid PDF region bounds')
    if (anchor['width'] <= 0 or anchor['height'] <= 0 or
            anchor['x'] + anchor['width'] > 1 or anchor['y'] + anchor['height'] > 1):
        raise ValueError('Invalid PDF region bounds')
    if not isinstance(anchor['source_version_id'], str) or not isinstance(anchor['source_hash'], str):
        raise ValueError('Invalid PDF region source')
    link = db.execute('SELECT * FROM source_works WHERE source=? AND work=?',
                      (anchor['source_version_id'], work_id)).fetchone()
    if not link:
        raise ValueError('Annotation source must be linked to this work')
    source = library.store.get(link['source'], db, 'source')
    if source['raw_hash'] != anchor['source_hash']:
        raise ValueError('PDF region source hash mismatch')
    # The raster frame remains valid for rotated/cropped pages even when native
    # extracted-text overlays are disabled. Render verifies the immutable bytes.
    preview_page(library.workspace, source, anchor['page'])
    return {**anchor, 'verified_against_local_source': False}, link['version']
