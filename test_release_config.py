"""Dependency-free checks of public origin configuration; no application startup."""
import ast
from pathlib import Path
import unittest
from urllib.parse import urlsplit

source = ast.parse(Path(__file__).with_name('app.py').read_text())
function = next(n for n in source.body if isinstance(n, ast.FunctionDef) and n.name == 'configured_origins')
namespace = {'urlsplit': urlsplit}
exec(compile(ast.Module(body=[function], type_ignores=[]), 'app.py', 'exec'), namespace)
configured_origins = namespace['configured_origins']

class ReleaseConfiguration(unittest.TestCase):
    def test_empty_is_fail_closed(self):
        self.assertEqual(configured_origins(''), frozenset())
    def test_explicit_https_origins(self):
        self.assertEqual(configured_origins('https://pi.example.test:9443, https://localhost:8443'),
                         frozenset({'https://pi.example.test:9443', 'https://localhost:8443'}))
    def test_unsafe_values_are_rejected(self):
        for value in ('*', 'https://*.example.test', 'http://localhost:8765', 'https://pi.example.test/path',
                      'https://user:password@pi.example.test', 'https://pi.example.test?x=1',
                      'https://pi.example.test#x', 'https://pi.example.test:bad', 'https://'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                configured_origins(value)

if __name__ == '__main__':
    unittest.main()
