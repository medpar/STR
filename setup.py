from openai import OpenAI

client = OpenAI()

# Create a new, empty vector store
vector_store = client.vector_stores.create(
    name="UniSubjectNotes"
)
VECTOR_STORE_ID = vector_store.id
print("Your vector_store_id:", VECTOR_STORE_ID)
