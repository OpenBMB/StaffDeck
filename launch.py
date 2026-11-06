"""Start an exported source/Docker installation after preparing its dependencies."""
import os
from pathlib import Path
import sys

root = Path(__file__).resolve().parent
sys.path[:0] = [str(root / 'backend'), str(root / 'backend/src')]
os.environ.setdefault('STAFFDECK_DISTRIBUTION_MANIFEST', str(root / 'distribution/manifest.json'))
from staffdeck_harness.distribution.bootstrap import configure_environment
configure_environment()
if __name__ == '__main__':
    import uvicorn
    uvicorn.run('single_port_app:app', host=os.environ.get('ULTRARAG_HOST', '127.0.0.1'),
                port=int(os.environ.get('ULTRARAG_PORT', '5173')))
