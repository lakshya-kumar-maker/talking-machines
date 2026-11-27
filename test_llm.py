import os

from dotenv import load_dotenv

load_dotenv()

GEMINI_MODEL = "gemini-3.8-flash"
GROQ_MODEL = "openai/gpt-oss-120b"


try:
    from google import genai

    g = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    r = g.models.generate_content(model=GEMINI_MODEL, contents="Say hi in 3 words")
    print("GEMINI OK |", r.text.strip())
except Exception as e:
    print("GEMINI FAILED:", e)

try:
    from groq import Groq

    q = Groq(api_key=os.environ["GROQ_API_KEY"])
    r = q.chat.completions.create(model=GROQ_MODEL, messages=[{"role": "user", "content": "Say hi in 3 words"}])
    print("GROQ OK   |", r.choices[0].message.content.strip())
except Exception as e:
    print("GROQ FAILED:", e)
