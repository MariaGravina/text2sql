import os
from google import genai


client = genai.Client(api_key=API_KEY)

print("🔍 Modelli disponibili per la generazione di testo:\n")
for model in client.models.list():
    # Filtra i modelli supportati per la chat/generazione
    if "generateContent" in getattr(model, "supported_actions", []):
        print(f"• {model.name}")