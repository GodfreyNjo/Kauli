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


def rows(job, side: str = "source") -> list[dict]:
    """One dict per speech segment: start/end in ms, the speaker and the text of the chosen side. "source" is what was said
    (a human correction wins); "target" is the translation."""
    out = []
    for seg in getattr(job, "segments", []) or []:
        if getattr(seg, "segment_type", "speech") != "speech":
            continue
        if side == "target":
            text = (getattr(seg, "final_text", "") or "").strip()
        else:
            text = (getattr(seg, "source_final_text", "") or getattr(seg, "source_transcript", "") or "").strip()
        if text:
            out.append({"start": seg.start_ms, "end": seg.end_ms, "speaker": getattr(seg, "speaker_id", None) or "", "text": text})
    return out


def paragraphs(job, timestamps: bool = True, speakers: bool = True, split: str = "line", side: str = "source") -> list[str]:
    """The transcript as paragraphs. split: "line" is one per segment (the default), "speaker" one per run of the same
    speaker, "single" everything as one paragraph."""
    items = rows(job, side)
    if split == "single":
        return [" ".join(r["text"] for r in items)] if items else []
    if split == "speaker":
        groups: list[dict] = []
        for r in items:
            if groups and groups[-1]["speaker"] == r["speaker"]:
                groups[-1]["text"] += " " + r["text"]
            else:
                groups.append(dict(r))
        items = groups
    out = []
    for r in items:
        prefix = f"[{_clock(r['start'])}] " if timestamps else ""
        if speakers and r["speaker"]:
            prefix += f"{r['speaker']}: "
        out.append(prefix + r["text"])
    return out


def _lines(job, timestamps: bool = True) -> list[str]:
    """[(timestamp) Speaker: text] per speech segment, in order."""
    return paragraphs(job, timestamps=timestamps)


def to_txt(job, title: str, timestamps: bool = True, speakers: bool = True, split: str = "line", side: str = "source") -> bytes:
    body = "\n\n".join(paragraphs(job, timestamps, speakers, split, side))
    return f"{title}\n{'=' * min(len(title), 60)}\n\n{body}\n".encode("utf-8")


def to_docx(job, title: str, timestamps: bool = True, speakers: bool = True, split: str = "line", side: str = "source") -> bytes:
    def para(text: str, bold: bool = False) -> str:
        run_props = "<w:rPr><w:b/><w:sz w:val=\"32\"/></w:rPr>" if bold else ""
        return (f'<w:p><w:r>{run_props}<w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>')

    body_paragraphs = [para(title, bold=True)] + [para(line) for line in paragraphs(job, timestamps, speakers, split, side)]
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f'<w:body>{"".join(body_paragraphs)}</w:body></w:document>'
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


def to_csv(job, timestamps: bool = True, speakers: bool = True, side: str = "source") -> bytes:
    """start, end, speaker, text. UTF-8 with a byte-order mark so Excel keeps accents and other scripts."""
    import csv
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow((["start", "end"] if timestamps else []) + (["speaker"] if speakers else []) + ["text"])
    for r in rows(job, side):
        w.writerow(([_clock(r["start"]), _clock(r["end"])] if timestamps else []) + ([r["speaker"]] if speakers else []) + [r["text"]])
    return buf.getvalue().encode("utf-8-sig")


def to_pdf(job, title: str, timestamps: bool = True, speakers: bool = True, split: str = "line", side: str = "source") -> bytes:
    """A plain, readable A4 PDF written with the standard library. The built-in PDF fonts cover Western European scripts
    only, so a transcript in another script raises ValueError and the caller points to Word instead."""
    import textwrap
    lines: list[tuple[str, bool]] = [(title, True), ("", False)]
    for para in paragraphs(job, timestamps, speakers, split, side):
        lines += [(ln, False) for ln in (textwrap.wrap(para, width=88) or [""])] + [("", False)]
    try:
        encoded = [(text.encode("cp1252"), bold) for text, bold in lines]
    except UnicodeEncodeError as exc:
        raise ValueError("PDF is not available for this language; download Word or text instead") from exc

    def esc(b: bytes) -> bytes:
        return b.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")

    per_page = 48
    pages = [encoded[i:i + per_page] for i in range(0, len(encoded), per_page)] or [[]]
    objs: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        4: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
    }
    kids = []
    for n, page in enumerate(pages):
        page_id, content_id = 5 + n * 2, 6 + n * 2
        stream = b"BT 56 790 Td 15 TL\n"
        for text, bold in page:
            stream += (b"/F2 14 Tf " if bold else b"/F1 11 Tf ") + b"(" + esc(text) + b") Tj T*\n"
        stream += b"ET"
        objs[content_id] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
        objs[page_id] = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                         b"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents %d 0 R >>" % content_id)
        kids.append(b"%d 0 R" % page_id)
    objs[2] = b"<< /Type /Pages /Kids [" + b" ".join(kids) + b"] /Count %d >>" % len(pages)
    out = b"%PDF-1.4\n"
    offsets = {}
    for num in sorted(objs):
        offsets[num] = len(out)
        out += b"%d 0 obj\n" % num + objs[num] + b"\nendobj\n"
    xref = len(out)
    last = max(objs)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (last + 1)
    for num in range(1, last + 1):
        out += b"%010d 00000 n \n" % offsets[num]
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (last + 1, xref)
    return out
