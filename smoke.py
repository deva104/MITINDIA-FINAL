import json, mimetypes, os, sys, traceback
from pathlib import Path
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()
try:
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    part = types.Part.from_bytes(data=Path(sys.argv[1]).read_bytes(), mime_type=mimetypes.guess_type(sys.argv[1])[0] or "image/jpeg")
    prompt = 'Return JSON only: {"visible":"<one sentence describing this image>"}'
    cfg = types.GenerateContentConfig(response_mime_type="application/json")
    for model in ("gemini-3.6-flash", "gemini-flash-latest"):
        try:
            resp = client.models.generate_content(model=model, contents=[part, prompt], config=cfg)
            break
        except Exception as e:
            print(f"{model} failed: {e}")
    print(model, resp.text)
    print(json.loads(resp.text))
except Exception:
    traceback.print_exc()
