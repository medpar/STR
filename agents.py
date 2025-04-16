import os
import logging
import re
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")


if not OPENAI_API_KEY:
    logging.error("Falta API_KEY en el .env")
    exit(1)

def process_query(query: str) -> str:
    """
    Process an agent query using the OpenRouter API and return the broadcast text.
    """
    instructions = (
        "You are a professional radio broadcaster. Provide a natural, broadcast-style answer without any URLs, links, or references in your response. Answer in castillian spanish. Use european format for all dates and units. Your response should always be in plain text, DO NOT use markdown. Answer very very briefly in maximum one paragraph."
    )
    client = OpenAI()
    response = client.responses.create(
        model="gpt-4.1-mini",
        tools=[{"type": "web_search_preview"}],
        instructions=instructions,
        input=query
    )
    raw_text = response.output_text
    # Remove markdown links like ([text](url)) and surrounding whitespace
    processed_text = re.sub(r'\s*\(\[([^\\\]]+)\]\(([^)]+)\)\)\s*', r'', raw_text).strip()
    return processed_text

if __name__ == "__main__":
    # Simple CLI for testing agent queries
    while True:
        user_query = input("Enter your query (or 'exit' to quit): ")
        if user_query.lower() == "exit":
            break
        broadcast_response = process_query(user_query)
        print("\nBroadcast Response:\n")
        print(broadcast_response)
        print("\n")
