from __future__ import annotations
"""
Quick smoke tests for Qianfan OpenAI-compatible endpoints using current .env.
Tests:
- chat completion
- embedding
- (optional) multimodal/ocr via image if provided

Usage examples:
  python scripts/test_qianfan_models.py --chat "你好"
  python scripts/test_qianfan_models.py --embed "杭州师范大学在哪里"
  python scripts/test_qianfan_models.py --multi --image path/to/img.png --prompt "图里是什么?"

Requirements: .env populated with CAMPUS_RAG_OPENAI_API_KEY / BASE_URL and model names.
"""
import argparse
import base64
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI


def load_client() -> OpenAI:
    load_dotenv()
    api_key = os.getenv("CAMPUS_RAG_OPENAI_API_KEY")
    base_url = os.getenv("CAMPUS_RAG_OPENAI_BASE_URL")
    if not api_key or not base_url:
        raise RuntimeError("CAMPUS_RAG_OPENAI_API_KEY or BASE_URL missing; check your .env")
    return OpenAI(api_key=api_key, base_url=base_url)


def test_chat(client: OpenAI, prompt: str, model: str) -> None:
    print(f"[chat] model={model}")
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=200,
        temperature=0.2,
    )
    print(resp.choices[0].message.content)


def test_embed(client: OpenAI, text: str, model: str) -> None:
    print(f"[embed] model={model}")
    resp = client.embeddings.create(model=model, input=text)
    print(f"embedding dim={len(resp.data[0].embedding)} ok")


def encode_image(path: Path) -> str:
    data = path.read_bytes()
    return base64.b64encode(data).decode("utf-8")


def test_multimodal(client: OpenAI, prompt: str, image_path: Path, model: str) -> None:
    print(f"[multimodal] model={model}, image={image_path}")
    img_b64 = encode_image(image_path)
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
                ],
            }
        ],
        max_tokens=200,
    )
    print(resp.choices[0].message.content)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--chat", type=str, help="Chat prompt to test")
    parser.add_argument("--embed", type=str, help="Text to embed")
    parser.add_argument("--multi", action="store_true", help="Run multimodal test")
    parser.add_argument("--image", type=str, help="Image path for multimodal")
    parser.add_argument("--prompt", type=str, default="图中有什么?", help="Prompt for multimodal")
    args = parser.parse_args()

    client = load_client()
    chat_model = os.getenv("CAMPUS_RAG_OPENAI_MODEL", "")
    embed_model = os.getenv("CAMPUS_RAG_EMBEDDING_MODEL", "")
    multi_model = os.getenv("CAMPUS_RAG_MULTIMODAL_MODEL", "") or chat_model

    if args.chat:
        if not chat_model:
            raise RuntimeError("CAMPUS_RAG_OPENAI_MODEL not set")
        test_chat(client, args.chat, chat_model)

    if args.embed:
        if not embed_model:
            raise RuntimeError("CAMPUS_RAG_EMBEDDING_MODEL not set")
        test_embed(client, args.embed, embed_model)

    if args.multi:
        if not multi_model:
            raise RuntimeError("CAMPUS_RAG_MULTIMODAL_MODEL (or chat model) not set")
        if not args.image:
            raise RuntimeError("--image is required for multimodal test")
        image_path = Path(args.image)
        if not image_path.exists():
            raise RuntimeError(f"Image not found: {image_path}")
        test_multimodal(client, args.prompt, image_path, multi_model)

    if not any([args.chat, args.embed, args.multi]):
        parser.print_help()


if __name__ == "__main__":
    main()
