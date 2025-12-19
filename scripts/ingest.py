from __future__ import annotations

import argparse
from pathlib import Path
from rich import print

from app.config import get_settings
from app.rag import load_documents
from app.vectorstore import VectorStore


def main():
    parser = argparse.ArgumentParser(description="Build vector index from campus docs")
    parser.add_argument("--docs", type=str, default=None, help="Docs folder path")
    args = parser.parse_args()

    settings = get_settings()
    docs_path = Path(args.docs) if args.docs else settings.docs_path
    print(f"[bold green]Loading docs from[/] {docs_path}")

    chunks = load_documents(docs_path, settings.chunk_size, settings.chunk_overlap, settings.chunk_overlap_ratio)
    print(f"Prepared {len(chunks)} chunks")

    store = VectorStore(settings)
    store.build(chunks)
    print(f"[bold blue]Index built and saved to[/] {settings.index_path}")


if __name__ == "__main__":
    main()
