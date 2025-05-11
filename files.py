# files.py

#!/usr/bin/env python3
"""
PDF upload + question‑answering logic, isolated from the Flask app.

• Uploads a PDF to the OpenAI Files endpoint and keeps the mapping
  filename → file_id in memory.
• Adds each file to your vector store (indexing happens asynchronously).
• Answers questions with the file_search tool, focusing on the specified file.
• Provides concepts() and questions() helpers for the UI.
"""

import os
import logging
from typing import Dict, List

from openai import OpenAI
from config import OPENAI_MODEL_FILE_QA

log = logging.getLogger(__name__)

class PDFManager:
    """In‑memory PDF store + vector‑store indexing + file_search Q&A."""

    def __init__(self, upload_dir: str, vector_store_id: str):
        self.upload_dir = upload_dir
        os.makedirs(self.upload_dir, exist_ok=True)
        self.client = OpenAI()
        # Simple in-memory store. For persistence, consider a DB or file.
        self._store: Dict[str, str] = {}  # filename → file_id
        self.vector_store_id = vector_store_id
        # Keep track of the currently "active" filename for context
        self.current_filename: str | None = None

    def upload(self, werkzeug_file) -> dict:
        """
        1. Save PDF locally
        2. Upload to OpenAI Files (purpose="assistants")
        3. Add to vector store
        4. Set as the current file for context
        Returns {"file_id": ..., "filename": ...}
        (Indexing is kicked off but not awaited.)
        """
        if not werkzeug_file or not werkzeug_file.filename.lower().endswith(".pdf"):
            raise ValueError("A PDF file is required.")

        filename = werkzeug_file.filename
        local_path = os.path.join(self.upload_dir, filename)
        werkzeug_file.save(local_path)
        log.info("Saved PDF locally: %s", local_path)

        # 1) Upload to Files API
        if filename in self._store:
            file_id = self._store[filename]
            log.info("PDF %s already uploaded (file_id=%s). Skipping upload.", filename, file_id)
        else:
            try:
                with open(local_path, "rb") as f:
                    openai_file = self.client.files.create(file=f, purpose="assistants")
                file_id = openai_file.id
                self._store[filename] = file_id
                log.info("Uploaded PDF %s → file_id=%s", filename, file_id)
            except Exception as e:
                log.error("Failed to upload PDF %s to OpenAI: %s", filename, e)
                raise ValueError(f"Failed to upload PDF to OpenAI: {e}")

        # 2) Add to Vector Store
        try:
            self.client.vector_stores.files.create(
                vector_store_id=self.vector_store_id,
                file_id=file_id
            )
            log.info("Added file_id=%s to vector store %s. Indexing started.", file_id, self.vector_store_id)
        except Exception as e:
            if "already attached" in str(e).lower():
                log.warning("File %s (file_id=%s) already in vector store %s.", filename, file_id, self.vector_store_id)
            else:
                log.error("Failed to add file_id=%s to vector store %s: %s", file_id, self.vector_store_id, e)

        # 3) Set this as the current file
        self.current_filename = filename
        log.info("Set current active PDF to: %s", filename)

        return {"file_id": file_id, "filename": filename}

    def _get_file_context_prompt(self, filename: str) -> str:
        """Generates the context part of the prompt."""
        if not filename:
            log.warning("No specific filename provided for context.")
            return ""
        if filename not in self._store and not os.path.exists(os.path.join(self.upload_dir, filename)):
            log.error("Filename '%s' requested for context does not exist.", filename)
            raise ValueError(f"Cannot generate context for non-existent file: {filename}")
        return f"Referencing the document named '{filename}'. "

    def ask(self, filename: str, question: str) -> str:
        """
        Ask a free‑form question using Responses API + file_search tool,
        strongly biasing towards the specified filename via prompt context.
        """
        if not filename:
            raise ValueError("Filename must be provided to 'ask'.")
        if filename not in self._store:
            local_path = os.path.join(self.upload_dir, filename)
            if os.path.exists(local_path):
                log.warning("File '%s' exists locally but not in session store. Re-upload might be needed.", filename)
            else:
                raise ValueError(f"File '{filename}' not found. Upload first.")

        fs_tool = {
            "type": "file_search",
            "vector_store_ids": [self.vector_store_id],
            "max_num_results": 5
        }

        context_prompt = self._get_file_context_prompt(filename)
        system_prompt = "Provide a natural, answer without any URLs, links, or references in your response. Answer in castillian spanish. Use european format for all dates and units. Your response should always be in plain text, DO NOT use markdown. Answer very very briefly in maximum one paragraph."
        full_question = f"{context_prompt} {system_prompt} Answer the following question: {question}"
        log.info("Asking OpenAI with contextual question: %s", full_question)

        try:
            resp = self.client.responses.create(
                model=OPENAI_MODEL_FILE_QA,
                input=full_question,
                tools=[fs_tool]
            )
        except Exception as e:
            log.error("OpenAI API call failed for 'ask': %s", e)
            raise RuntimeError(f"Failed to get answer from OpenAI: {e}")

        texts: List[str] = []
        if resp.output:
            for out in resp.output:
                if getattr(out, "type", None) == "message":
                    for block in out.content:
                        if getattr(block, "type", None) == "output_text":
                            texts.append(block.text)
        else:
            log.warning("OpenAI response contained no output messages.")
            return "Sorry, I couldn't retrieve an answer for that."

        return " ".join(texts).strip() if texts else "No text answer found in the response."

    def _generate_from_pdf(self, filename: str, prompt: str) -> List[str]:
        raw_answer = self.ask(filename, prompt)
        lines = [
            line.strip(" \u2022-0123456789. ")
            for line in raw_answer.splitlines()
            if line.strip()
        ]
        return lines

    def concepts(self, filename: str) -> List[str]:
        prompt = "List the key concepts discussed in this document, one per line."
        return self._generate_from_pdf(filename, prompt)

    def questions(self, filename: str) -> List[str]:
        prompt = "Generate five test questions based on the content of this document, one per line."
        return self._generate_from_pdf(filename, prompt)

    def get_current_filename(self) -> str | None:
        return self.current_filename

    def clear_current_filename(self):
        self.current_filename = None
        log.info("Cleared current active PDF filename.")
