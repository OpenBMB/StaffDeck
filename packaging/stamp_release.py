"""Apply a validated release version to the public product manifest."""
import argparse
import json
from pathlib import Path

from source_release import version_value

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('version', nargs='?', default='')
args = parser.parse_args()
path = Path(__file__).resolve().parents[1] / 'distribution/manifest.json'
value = json.loads(path.read_text()); value['version'] = version_value(args.version or value['version'])
path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')

print("VERSION=" + value["version"])
