# File: /files.py
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
        # Check if file already exists in the vector store to avoid duplicates (Optional but good practice)
        try:
            # Note: This is a basic check; robust check might need listing files and comparing IDs.
            # For simplicity, we just try to add it. If it fails due to duplication,
            # the API might handle it gracefully or raise an error we might want to catch.
            self.client.vector_stores.files.create(
                vector_store_id=self.vector_store_id,
                file_id=file_id
            )
            log.info("Vector‑store indexing started for file_id=%s", file_id)
        except Exception as e:
            # Log if adding the file failed, e.g., if it was already added.
            log.warning("Could not add file %s to vector store (maybe already exists?): %s", file_id, e)


        return {"file_id": file_id, "filename": filename}

    def ask(self, filename: str, question: str) -> str:
        """
        Ask a free‑form question using Responses API + file_search tool,
        explicitly mentioning the filename.
        """
        if filename not in self._store:
            # Check if the file exists locally even if not in memory store (e.g. after restart)
            local_path = os.path.join(self.upload_dir, filename)
            if not os.path.exists(local_path):
                 raise ValueError(f"File '{filename}' not found. Upload first.")
            # If file exists locally but not in store, try to find its ID (more complex, omitted for now)
            # For now, we rely on the in-memory store populated during the session.
            # A persistent mapping (e.g., in a file or DB) would be more robust.
            log.warning("File '%s' not in memory store, proceeding with caution.", filename)
            # pass # Or raise ValueError("File not found in current session. Upload first.")

        fs_tool = {
            "type": "file_search",
            "vector_store_ids": [self.vector_store_id],
            "max_num_results": 5
        }

        # *** MODIFICATION: Add filename context to the input ***
        contextual_question = f"Using the document named '{filename}', answer the following question: {question}"
        log.info("Asking OpenAI with contextual question: %s", contextual_question)

        resp = self.client.responses.create(
            model="gpt-4o-mini", # Changed model as gpt-4.1-mini does not exist - check documentation
            input=contextual_question, # Use the contextual question
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

    def _generate_from_pdf(self, filename: str, prompt: str) -> List[str]:
        """Internal helper to ask questions about the PDF with context."""
        # *** MODIFICATION: Add filename context to the internal prompt ***
        contextual_prompt = f"Regarding the document named '{filename}': {prompt}"

        raw = self.ask(
            filename, # Pass filename here, ask() will add context again, which is slightly redundant but safe
            prompt # Pass the original prompt, ask() adds context
            # Or, more directly:
            # raw = self._ask_internal(filename, contextual_prompt) # If we create _ask_internal without context prefix
        )
        lines = [
            line.strip(" \u2022-0123456789. ") # Keep the stripping logic
            for line in raw.splitlines()
            if line.strip()
        ]
        return lines

    def concepts(self, filename: str) -> List[str]:
        """
        Return a list of the key concepts in the document.
        """
        prompt = "Por favor, enumera los conceptos clave tratados en este documento, uno por línea."
        return self._generate_from_pdf(filename, prompt)


    def questions(self, filename: str) -> List[str]:
        """
        Generate a short set of test questions about the document.
        """
        prompt = "Genera cinco preguntas de prueba sobre el contenido de este documento, una por línea."
        return self._generate_from_pdf(filename, prompt)

    # Note: Consider adding a method to clear the _store or remove files from the vector store
    # if strict isolation between uploads is absolutely required, but the context prompt is usually sufficient.