import sys
from pathlib import Path

# Make the resource directory importable so tests can do `from concourse import ...`
sys.path.insert(0, str(Path(__file__).parent))
