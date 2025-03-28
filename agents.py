# agents.py
import os
import logging
from dotenv import load_dotenv
from openai import OpenAI  # Usando el paquete OpenAI en modo OpenRouter

load_dotenv()

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_API_BASE = os.getenv("OPENROUTER_API_BASE", "https://openrouter.ai/api/v1")

if not OPENROUTER_API_KEY:
    logging.error("Falta OPENROUTER_API_KEY en el .env")
    exit(1)

def process_query(query: str) -> str:
    system_prompt = (
        "You are a professional radio broadcaster. Provide a natural, broadcast-style answer without any URLs, links, or references in your response."
    )
    # test
    # Llamada a OpenRouter usando la sintaxis exacta proporcionada:
    client = OpenAI(
        base_url=OPENROUTER_API_BASE,
        api_key=OPENROUTER_API_KEY,
    )
    completion = client.chat.completions.create(
        #extra_headers={},
        #extra_body={},
        model="deepseek/deepseek-chat",
        # plugins=[{
        #     "id":"web",
        #     "max_results": 5,
        #     "search_prompt": "A web search was conducted on `date`. Incorporate the following web search results into your response. IMPORTANT: DO NOT Cite them using markdown links."
        # }],
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": query}
        ]
    )
    response_text = completion.choices[0].message.content.strip()
    return response_text

if __name__ == "__main__":
    while True:
        user_query = input("Enter your query (or 'exit' to quit): ")
        if user_query.lower() == "exit":
            break
        broadcast_response = process_query(user_query)
        print("\nBroadcast Response:\n")
        print(broadcast_response)
        print("\n")
