"""Keep the gateway's DDP-Layout import path backed by the shared core."""

from ddp_core.application.layout import (  # noqa: F401
    BLOCK_TYPES,
    CODE_DETECTION,
    CODE_DETECTION_STATES,
    ENGINE_NOTES,
    LAYOUT_VERSION,
    PROMISED_BLOCK_FIELDS,
    PROMISED_PAGE_FIELDS,
    _MINERU_TYPE_MAP,
    _normalize_block,
    block_text,
    build,
    build_pages,
    from_mineru,
    normalize_type,
    page_count,
    table_html,
    validate,
)
