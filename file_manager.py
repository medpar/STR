#!/usr/bin/env python3
"""
PDF upload + question‑answering logic, isolated from the Flask app.

• Uploads a PDF to the OpenAI Files endpoint and keeps the mapping
  filename → file_id in memory (could be persisted if needed).
• Answers questions with the File‑search tool (beta) following:
  https://platform.openai.com/docs/guides/pdf-files
"""

from __future__ import annotations

import os
import logging
from typing import Dict

from openai import OpenAI

log = logging.getLogger(__name__)


class PDFManager:
    """Simple in‑memory PDF store."""

    def __init__(self, upload_dir: str):
        self.upload_dir = upload_dir
        os.makedirs(self.upload_dir, exist_ok=True)
        self.client = OpenAI()
        self._store: Dict[str, str] = {}  # filename → file_id

    # ------------------------------------------------------------------#
    #  Upload                                                           #
    # ------------------------------------------------------------------#
    def upload(self, werkzeug_file) -> dict:
        """
        Save PDF locally and upload to OpenAI. Returns dict with {file_id, filename}.
        """
        if not werkzeug_file or not werkzeug_file.filename.lower().endswith(".pdf"):
            raise ValueError("A PDF file is required.")

        save_path = os.path.join(self.upload_dir, werkzeug_file.filename)
        werkzeug_file.save(save_path)

        with open(save_path, "rb") as f:
            openai_file = self.client.files.create(file=f, purpose="assistants")

        self._store[werkzeug_file.filename] = openai_file.id
        log.info("PDF uploaded: %s → %s", werkzeug_file.filename, openai_file.id)
        return {"file_id": openai_file.id, "filename": werkzeug_file.filename}

    # ------------------------------------------------------------------#
    #  Ask                                                              #
    # ------------------------------------------------------------------#
    def ask(self, filename: str, question: str) -> str:
        """
        Query the uploaded PDF with file_search tool and return the text answer.
        """
        if filename not in self._store:
            raise ValueError("File not found. Upload first.")

        file_id = self._store[filename]

        completion = self.client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[{"role": "user", "content": question}],
            tools=[{"type": "file_search"}],
            file_ids=[file_id],
        )
        answer = completion.choices[0].message.content
        log.debug("PDF answer: %s", answer)
        return answer
