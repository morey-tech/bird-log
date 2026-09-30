import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('dependency_check', Path(__file__).resolve().parents[1] / 'check-dependency-files.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class DependencyDiscoveryTests(unittest.TestCase):
    def check_reference(self, name, exists=True):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = root / 'services/example'
            service.mkdir(parents=True)
            (service / 'requirements.txt').write_text(f'-c {name}\n-e .\n')
            if exists:
                (service / name).write_text('example==1.0\n')
            return module.check(root)

    def test_txt_constraint_is_discoverable(self):
        self.assertEqual(self.check_reference('constraints.txt'), [])

    def test_lock_constraint_is_rejected_even_when_present(self):
        self.assertIn('Dependabot cannot stage', self.check_reference('requirements.lock')[0])

    def test_missing_constraint_is_rejected(self):
        self.assertIn('missing', self.check_reference('constraints.txt', exists=False)[0])


if __name__ == '__main__':
    unittest.main()
