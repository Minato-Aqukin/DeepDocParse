"""Compatibility imports for the shared, infrastructure-free CPU PDF parser.

Gateway engine registration and HTTP dispatch stay here; PDF parsing and display
coordinates have one implementation for server and local execution.
"""

from ddp_core.application.borndigital import (  # noqa: F401
    MIN_HORIZONTAL_OVERLAP,
    PARAGRAPH_GAP_RATIO,
    _CODE_SYMBOLS,
    _CODE_TOKEN,
    _MONO_FONT_PARTS,
    _char_fonts,
    _font_name,
    _fonts_in_rect,
    _horizontal_overlap,
    _lines_of_page,
    _looks_like_code,
    _merge_lines,
    _to_display_bbox,
    extract_pages,
    to_markdown,
)
