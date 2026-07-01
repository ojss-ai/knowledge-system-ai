"""Writes openapi.json for frontend codegen: python -m app.scripts.export_openapi"""

import json
from pathlib import Path

from app.main import create_app

Path("openapi.json").write_text(json.dumps(create_app().openapi(), indent=2))
print("wrote backend/openapi.json")
