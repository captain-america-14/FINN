from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

PACKAGES = [
    "streamlit", "pandas", "numpy", "python-dotenv", "yfinance", "bse", "requests",
    "beautifulsoup4", "google-genai", "pypdf", "sentence-transformers", "faiss-cpu", "torch",
]

lines = ["--extra-index-url https://download.pytorch.org/whl/cpu", ""]
for name in PACKAGES:
    try:
        lines.append(f"{name}=={version(name).split('+')[0]}")
    except PackageNotFoundError:
        print("NOT INSTALLED locally, skipped:", name)

Path("requirements.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
print("\n".join(lines))