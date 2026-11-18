from google import genai

from app.config import settings

print("key loaded:", bool(settings.gemini_api_key))
print("model:", settings.gemini_model)

client = genai.Client(api_key=settings.gemini_api_key)
response = client.models.generate_content(
    model=settings.gemini_model,
    contents="Reply with exactly: GreenScale connected",
)

print("text:", repr(response.text))