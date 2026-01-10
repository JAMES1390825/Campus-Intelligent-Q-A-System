from __future__ import annotations

from rich import print

from app.config import get_settings
from app.rag import load_documents
from app.vectorstore import VectorStore
from app.document_storage import DocumentStorage


def main():
    settings = get_settings()
    storage = DocumentStorage(settings)
    docs_payload = list(storage.stream_documents())
    if not docs_payload:
        print("[bold yellow]No documents found in database; upload via admin portal first[/]")
        return

    chunks = load_documents(docs_payload, settings.chunk_size, settings.chunk_overlap, settings.chunk_overlap_ratio)
    print(f"Prepared {len(chunks)} chunks")

    store = VectorStore(settings)
    store.build(chunks)
    print("[bold blue]Vector index rebuilt in PgVector[/]")


if __name__ == "__main__":
    main()
