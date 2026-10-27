"""Plain-text and Word (.docx) transcript exports, built on demand from a
finished job's segments - the formats people expect from a transcription
service, next to the existing SRT. No new dependencies: the .docx is a tiny,
valid WordprocessingML package written with the standard library.

Text used is the reviewed source text where a human edited it
(source_final_text), else the raw ASR text - the same precedence the editor
and the SRT use. Timing is per segment (line), which is all the ASR provider
returns; nothing here pretends to be per-word."""
from __future__ import annotations

import io
import zipfile
from xml.sax.saxutils import escape


def _clock(ms: int) -> str:
    total = max(0, int(ms // 1000))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _lines(job, timestamps: bool = True) -> list[str]:
    """[(timestamp) Speaker: text] per speech segment, in order."""
    out = []
    for seg in getattr(job, "segments", []) or []:
        if getattr(seg, "segment_type", "speech") != "speech":
            continue
        text = (getattr(seg, "source_final_text", "") or getattr(seg, "source_transcript", "") or "").strip()
        if not text:
            continue
        prefix = f"[{_clock(seg.start_ms)}] " if timestamps else ""
        speaker = getattr(seg, "speaker_id", None)
        if speaker:
            prefix += f"{speaker}: "
        out.append(prefix + text)
    return out


def to_txt(job, title: str, timestamps: bool = True) -> bytes:
    body = "\n\n".join(_lines(job, timestamps))
    return f"{title}\n{'=' * min(len(title), 60)}\n\n{body}\n".encode("utf-8")


def to_docx(job, title: str, timestamps: bool = True) -> bytes:
    def para(text: str, bold: bool = False) -> str:
        run_props = "<w:rPr><w:b/><w:sz w:val=\"32\"/></w:rPr>" if bold else ""
        return (f'<w:p><w:r>{run_props}<w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>')

    paragraphs = [para(title, bold=True)] + [para(line) for line in _lines(job, timestamps)]
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f'<w:body>{"".join(paragraphs)}</w:body></w:document>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '</Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/></Relationships>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", document)
    return buf.getvalue()
