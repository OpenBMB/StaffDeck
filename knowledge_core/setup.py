from setuptools import setup
from setuptools.command.build_py import build_py as _build_py


class build_py(_build_py):
    # These modules are retained in the source checkout only for historical
    # fixture replay and read-only conversion tests. They are not part of the
    # independently deployable product and must never be importable from its wheel.
    legacy = {
        'knowledge_core.service', 'knowledge_core.full_runtime', 'knowledge_core.full_api',
        'knowledge_core.okf', 'knowledge_core.versions', 'knowledge_core.publication',
    }

    def build_module(self, module, module_file, package):
        if f'{package}.{module}' in self.legacy:
            return
        return super().build_module(module, module_file, package)

    def find_package_modules(self, package, package_dir):
        modules = super().find_package_modules(package, package_dir)
        return [row for row in modules if f'{package}.{row[1]}' not in self.legacy]


setup(cmdclass={'build_py': build_py})
