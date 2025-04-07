import os
import logging
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_API_BASE = os.getenv("OPENROUTER_API_BASE", "https://openrouter.ai/api/v1")

if not OPENROUTER_API_KEY:
    logging.error("Falta OPENROUTER_API_KEY en el .env")
    exit(1)

def process_query(query: str) -> str:
    """
    Process an agent query using the OpenRouter API and return the broadcast text.
    """
    system_prompt = (
        "You are a professional radio broadcaster. Provide a natural, broadcast-style answer without any URLs, links, or references in your response. Answer in spanish from Spain. Use european format for all dates and units. Your response should always be in plain text, DO NOT use markdown. Answer in one or two paragraphs. "
    )
    client = OpenAI(
        base_url=OPENROUTER_API_BASE,
        api_key=OPENROUTER_API_KEY,
    )
    completion = client.chat.completions.create(
        model="openai/gpt-4o-mini:online",
        #model="deepseek/deepseek-v3-base:free",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": query}
        ]
    )
    response_text = completion.choices[0].message.content.strip()
    return response_text

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
