"""Run existing pure template guards without importing DB-backed engine.views."""
import ast
import unittest
from pathlib import Path

root = Path(__file__).resolve().parents[1]
source = root / 'engine/tests.py'
module = ast.parse(source.read_text())
names = {'DashboardSetupTemplateTests', 'DashboardReferralTemplateTests', 'DashboardPwaLayoutTemplateTests'}
selected = ast.Module(body=[node for node in module.body if isinstance(node, ast.ClassDef) and node.name in names], type_ignores=[])
namespace = {'SimpleTestCase': unittest.TestCase, 'Path': Path}
exec(compile(selected, str(source), 'exec'), namespace)
suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(namespace[name]) for name in sorted(names))
raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
