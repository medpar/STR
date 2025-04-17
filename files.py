#!/usr/bin/env python3
"""
PDF upload + question‑answering logic, isolated from the Flask app.

• Uploads a PDF to the OpenAI Files endpoint and keeps the mapping
  filename → file_id in memory.
• Adds each file to your vector store (indexing happens asynchronously).
• Answers questions with the file_search tool.
• Provides `concepts()` and `questions()` helpers for the UI.
"""

import os
import logging
from typing import Dict, List

from openai import OpenAI

log = logging.getLogger(__name__)


class PDFManager:
    """In‑memory PDF store + vector‑store indexing + file_search Q&A."""

    def __init__(self, upload_dir: str, vector_store_id: str):
        self.upload_dir = upload_dir
        os.makedirs(self.upload_dir, exist_ok=True)
        self.client = OpenAI()
        self._store: Dict[str, str] = {}  # filename → file_id
        self.vector_store_id = vector_store_id

    def upload(self, werkzeug_file) -> dict:
        """
        1. Save PDF locally
        2. Upload to OpenAI Files (purpose="assistants")
        3. Add to vector store
        Returns {"file_id": ..., "filename": ...}
        (Indexing is kicked off but not awaited.)
        """
        if not werkzeug_file or not werkzeug_file.filename.lower().endswith(".pdf"):
            raise ValueError("A PDF file is required.")

        filename = werkzeug_file.filename
        local_path = os.path.join(self.upload_dir, filename)
        werkzeug_file.save(local_path)

        # 1) Upload to Files API
        with open(local_path, "rb") as f:
            openai_file = self.client.files.create(file=f, purpose="assistants")
        file_id = openai_file.id
        self._store[filename] = file_id
        log.info("Uploaded PDF %s → file_id=%s", filename, file_id)

        # 2) Add to Vector Store (indexing runs in background)
        self.client.vector_stores.files.create(
            vector_store_id=self.vector_store_id,
            file_id=file_id
        )
        log.info("Vector‑store indexing started for file_id=%s", file_id)

        return {"file_id": file_id, "filename": filename}

    def ask(self, filename: str, question: str) -> str:
        """
        Ask a free‑form question using Responses API + file_search tool.
        """
        if filename not in self._store:
            raise ValueError("File not found. Upload first.")

        fs_tool = {
            "type": "file_search",
            "vector_store_ids": [self.vector_store_id],
            "max_num_results": 5
        }

        resp = self.client.responses.create(
            model="gpt-4.1-mini",
            input=question,
            tools=[fs_tool]
        )

        # collect all output_text chunks from the assistant message
        texts: List[str] = []
        for out in resp.output:
            # out.type is e.g. "file_search_call" or "message"
            if getattr(out, "type", None) == "message":
                # out.content is a list of content blocks
                for block in out.content:
                    if getattr(block, "type", None) == "output_text":
                        # block.text contains the actual assistant text
                        texts.append(block.text)

        return " ".join(texts).strip()

    def concepts(self, filename: str) -> List[str]:
        """
        Return a list of the key concepts in the document.
        """
        raw = self.ask(
            filename,
            "Por favor, enumera los conceptos clave tratados en este documento, uno por línea."
        )
        lines = [
            line.strip(" \u2022-0123456789. ")
            for line in raw.splitlines()
            if line.strip()
        ]
        return lines

    def questions(self, filename: str) -> List[str]:
        """
        Generate a short set of test questions about the document.
        """
        raw = self.ask(
            filename,
            "Genera cinco preguntas de prueba sobre el contenido de este documento, una por línea."
        )
        lines = [
            line.strip(" \u2022-0123456789. ")
            for line in raw.splitlines()
            if line.strip()
        ]
        return lines
