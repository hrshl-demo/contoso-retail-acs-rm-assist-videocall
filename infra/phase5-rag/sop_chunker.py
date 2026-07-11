"""
infra/phase5-rag/sop_chunker.py

Chunks SOP markdown by '##' section boundaries. Robust to the heading styles the
Foundry generator produces: handles both numbered ('## 3. Title') and unnumbered
('## Title') H2 headings. Each chunk gets a stable ID derived from file + ordinal,
so re-indexing the same content yields the same IDs (idempotent upserts).
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SopChunk:
    chunk_id: str
    sop_id: str
    sop_title: str
    section_number: int
    section_title: str
    content: str


# Matches '## 3. Title' OR '## Title' (captures optional leading number).
SECTION_RE = re.compile(r"^##\s+(?:(\d+)\.\s+)?(.+?)\s*$", re.MULTILINE)
TITLE_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


def chunk_sop(path: Path) -> list[SopChunk]:
    text = path.read_text()
    sop_id = path.stem
    title_match = TITLE_RE.search(text)
    sop_title = title_match.group(1).strip() if title_match else sop_id

    matches = list(SECTION_RE.finditer(text))
    if not matches:
        return [SopChunk(
            chunk_id=f"{sop_id}_section-0", sop_id=sop_id, sop_title=sop_title,
            section_number=0, section_title="(whole document)", content=text.strip(),
        )]

    chunks: list[SopChunk] = []
    for i, m in enumerate(matches):
        # use explicit number if present, else fall back to running ordinal
        section_num = int(m.group(1)) if m.group(1) else (i + 1)
        section_title = m.group(2).strip()
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        content = text[start:end].strip()
        chunks.append(SopChunk(
            chunk_id=f"{sop_id}_section-{i+1}",   # ordinal for stable uniqueness
            sop_id=sop_id, sop_title=sop_title,
            section_number=section_num, section_title=section_title, content=content,
        ))
    return chunks


def chunk_all_sops(sop_dir: Path) -> list[SopChunk]:
    out: list[SopChunk] = []
    for p in sorted(sop_dir.glob("*.md")):
        out.extend(chunk_sop(p))
    return out
