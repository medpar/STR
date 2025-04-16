import re
from openai import OpenAI
client = OpenAI()

response = client.responses.create(
    model="gpt-4.1-mini",
    tools=[{"type": "web_search_preview"}],
    instructions="You are a professional radio broadcaster. Provide a natural, broadcast-style answer without any URLs, links, or references in your response. Answer in castillian spanish. Use european format for all dates and units. Your response should always be in plain text, DO NOT use markdown. Answer very very briefly in maximum one paragraph.",
    input="Dime la fecha exacta de hoy y las noticias más relevantes en Valladolid, España.",
)

processed_text = re.sub(r'\s*\(\[([^\\\]]+)\]\(([^)]+)\)\)\s*', r'', response.output_text)
print(processed_text)