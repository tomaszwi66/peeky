"""
Ephemeral local RAG for Peeky.

A small in-memory retriever: attach a .txt/.md/.pdf file, the contents are
chunked, embedded via Ollama (`nomic-embed-text` by default), and stored in
RAM. The next user question is augmented with the top-K most relevant chunks.

Nothing is persisted. The store dies with the process, and the user can also
clear it manually from the right-click menu.

If the embedding model is not pulled in Ollama, we fall back to a simple
keyword overlap score so the feature degrades gracefully instead of erroring.
"""

from __future__ import annotations
import os, re, math, logging, threading
from typing import List, Tuple

import ollama

log = logging.getLogger("peeky.rag")


def _read_text_file(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def _read_pdf(path: str) -> str:
    """Extract text from a PDF. Tries pypdf; returns "" if unavailable."""
    try:
        from pypdf import PdfReader
    except Exception as e:
        log.warning("pypdf not installed: %s", e)
        return ""
    try:
        reader = PdfReader(path)
        out = []
        for page in reader.pages:
            try:
                out.append(page.extract_text() or "")
            except Exception:
                pass
        return "\n".join(out)
    except Exception as e:
        log.error("PDF read failed: %s", e)
        return ""


def load_document(path: str) -> str:
    """Return the document text, or "" if the type is unsupported."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".txt", ".md", ".markdown", ".log", ".csv", ".json", ".py",
               ".js", ".ts", ".html", ".xml", ".yaml", ".yml", ".rst"):
        return _read_text_file(path)
    if ext == ".pdf":
        return _read_pdf(path)
    # Last-resort: try as plain text.
    try:
        return _read_text_file(path)
    except Exception:
        return ""


def chunk_text(text: str, size: int = 600, overlap: int = 80) -> List[str]:
    """Split on paragraph boundaries first, then pack into ~size-char chunks."""
    text = re.sub(r"\r\n?", "\n", text).strip()
    if not text:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: List[str] = []
    buf = ""
    for p in paragraphs:
        if len(buf) + len(p) + 2 <= size:
            buf = (buf + "\n\n" + p).strip()
            continue
        if buf:
            chunks.append(buf)
        # If the paragraph itself is larger than `size`, hard-split it.
        if len(p) > size:
            step = max(1, size - overlap)
            for i in range(0, len(p), step):
                chunks.append(p[i:i + size])
            buf = ""
        else:
            buf = p
    if buf:
        chunks.append(buf)
    return chunks


def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na  = math.sqrt(sum(x * x for x in a))
    nb  = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


_WORD_RE = re.compile(r"[a-z0-9]{2,}")
def _bag(text: str) -> set:
    return set(_WORD_RE.findall(text.lower()))


def _keyword_score(query: str, chunk: str) -> float:
    q = _bag(query)
    if not q:
        return 0.0
    c = _bag(chunk)
    if not c:
        return 0.0
    return len(q & c) / math.sqrt(len(q) * len(c))


class EphemeralRAG:
    """Holds the current attachment in RAM. Single document at a time."""

    def __init__(self, embed_model: str = "nomic-embed-text"):
        self.embed_model = embed_model
        self.filename: str = ""
        self.chunks:   List[str] = []
        self.embeddings: List[List[float]] = []   # empty if fallback mode
        self.using_fallback = False
        self._lock = threading.Lock()

    # ----- mutation ---------------------------------------------------------
    def clear(self):
        with self._lock:
            self.filename = ""
            self.chunks = []
            self.embeddings = []
            self.using_fallback = False

    def attach(self, path: str, chunk_size: int = 600) -> Tuple[bool, str]:
        """Load + chunk + embed a document. Returns (ok, status_message)."""
        text = load_document(path)
        if not text.strip():
            return False, "No readable text in this file."

        chunks = chunk_text(text, size=chunk_size)
        if not chunks:
            return False, "Document is empty after chunking."

        embeddings: List[List[float]] = []
        fallback = False
        try:
            for ch in chunks:
                resp = ollama.embeddings(model=self.embed_model, prompt=ch)
                vec = resp.get("embedding") or []
                if not vec:
                    raise RuntimeError("empty embedding")
                embeddings.append(list(vec))
        except Exception as e:
            log.warning("Embedding model unavailable (%s); using keyword fallback", e)
            fallback = True
            embeddings = []

        with self._lock:
            self.filename = os.path.basename(path)
            self.chunks = chunks
            self.embeddings = embeddings
            self.using_fallback = fallback

        mode = "keyword fallback" if fallback else self.embed_model
        return True, f"Attached {self.filename} — {len(chunks)} chunks ({mode})"

    # ----- retrieval --------------------------------------------------------
    def is_active(self) -> bool:
        return bool(self.chunks)

    def status(self) -> str:
        if not self.is_active():
            return ""
        mode = "kw" if self.using_fallback else "emb"
        return f"📎 {self.filename} ({len(self.chunks)} chunks, {mode})"

    def retrieve(self, query: str, k: int = 4) -> List[str]:
        with self._lock:
            chunks = list(self.chunks)
            embs   = list(self.embeddings)
            fallback = self.using_fallback or not embs

        if not chunks:
            return []

        scored: List[Tuple[float, str]] = []
        if fallback:
            for ch in chunks:
                scored.append((_keyword_score(query, ch), ch))
        else:
            try:
                qresp = ollama.embeddings(model=self.embed_model, prompt=query)
                qvec = list(qresp.get("embedding") or [])
            except Exception as e:
                log.warning("Query embed failed (%s); falling back to keywords", e)
                qvec = []
            if not qvec:
                for ch in chunks:
                    scored.append((_keyword_score(query, ch), ch))
            else:
                for emb, ch in zip(embs, chunks):
                    scored.append((_cosine(qvec, emb), ch))

        scored.sort(key=lambda x: x[0], reverse=True)
        top = [c for s, c in scored[:k] if s > 0]
        return top or [c for _, c in scored[:k]]

    def augment_prompt(self, user_text: str, k: int = 4) -> str:
        """Return the user text with attached-document context prepended."""
        ctx = self.retrieve(user_text, k=k)
        if not ctx:
            return user_text
        joined = "\n\n---\n\n".join(ctx)
        return (
            f"You have access to the following excerpts from the user's attached "
            f"document '{self.filename}'. Use them when relevant; if they do not "
            f"answer the question, say so plainly.\n\n"
            f"=== ATTACHED CONTEXT ===\n{joined}\n=== END CONTEXT ===\n\n"
            f"User question: {user_text}"
        )
