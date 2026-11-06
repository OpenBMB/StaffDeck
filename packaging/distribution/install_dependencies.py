"""Install the same local SDK/modules for source, Docker and native builds."""
import json
from pathlib import Path
import subprocess
import sys
import tomllib

def install(root):
    root = Path(root).resolve()
    manifest = root / 'distribution/manifest.json'
    if not manifest.is_file():
        return
    sdk = root / 'harness-engine/python/sdk'
    if (sdk / 'pyproject.toml').is_file():
        project = tomllib.loads((sdk / 'pyproject.toml').read_text())['project']
        dependencies = project.get('dependencies', [])
        runtime_requirements = [value for value in dependencies if value.startswith('deepseek-harness-runtime-bin')]
        if runtime_requirements:
            runtime = root / 'harness-engine/python/sdk-runtime'
            if not (runtime / 'pyproject.toml').is_file():
                raise ValueError('Local Harness SDK runtime source is required')
            # The bridge launches the exported Node engine explicitly. Editable
            # runtime installs keep its resolver available without fetching an
            # unrelated platform wheel (or publishing any private source).
            subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-deps', '-e', str(runtime)], check=True)
        dependencies = [value for value in dependencies if value not in runtime_requirements]
        if dependencies:
            subprocess.run([sys.executable, '-m', 'pip', 'install', *dependencies], check=True)
        subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-deps', str(sdk)], check=True)
    for relative in json.loads(manifest.read_text()).get('module_packages', []):
        directory = (root / relative).resolve()
        if not directory.is_relative_to((root / 'extensions').resolve()):
            raise ValueError('Invalid distribution module path')
        subprocess.run([sys.executable, '-m', 'pip', 'install', str(directory)], check=True)


if __name__ == '__main__':
    install(Path(__file__).resolve().parents[2])
