"""An owned, disposable CPU process. Never starts network/model services."""

import hashlib
import json
import os
import signal
import sys


def main():
    # Linux parent-death signal prevents a crashed runtime leaving compute behind.
    if sys.platform == "linux":
        import ctypes

        parent = os.getppid()
        ctypes.CDLL(None).prctl(1, signal.SIGTERM)
        if parent == 1 or os.getppid() != parent:
            return 1
    try:
        from ddp_core.application.borndigital import extract_pages
        from ddp_core.application.layout import build

        from ddp_local.blobs import MAX_INPUT

        with os.fdopen(int(sys.argv[1]), "rb") as stream:
            pdf = stream.read(MAX_INPUT + 1)
        if len(pdf) > MAX_INPUT:
            raise ValueError("input_too_large")
        if hashlib.sha256(pdf).hexdigest() != sys.argv[2]:
            raise ValueError("input_changed")
        if not pdf.startswith(b"%PDF-"):
            raise ValueError("invalid_pdf")
        import pypdfium2

        document = pypdfium2.PdfDocument(pdf)
        try:
            if len(document) > 500:
                raise ValueError("page_budget_exceeded")
        finally:
            document.close()
        pages = extract_pages(pdf)
        if not pages or not any(p.get("blocks") for p in pages):
            raise ValueError("no_text_layer")
        result = build(pages, engine="borndigital", code_detection="heuristic")
        encoded = json.dumps({"layout": result}, ensure_ascii=False).encode()
        if len(encoded) > 32 * 1024 * 1024:
            raise ValueError("layout_too_large")
        sys.stdout.buffer.write(encoded)
        return 0
    except MemoryError:
        code = "out_of_memory"
    except ImportError:
        code = "cpu_provider_unavailable"
    except ValueError as exc:
        allowed = {
            "input_too_large",
            "input_changed",
            "invalid_pdf",
            "page_budget_exceeded",
            "no_text_layer",
            "layout_too_large",
        }
        code = str(exc) if str(exc) in allowed else "parse_failed"
    except Exception:
        code = "parse_failed"
    print(json.dumps({"error": code}))
    return 1


if __name__ == "__main__":
    sys.exit(main())
