"""Run existing pure template guards without importing DB-backed engine.views."""
import ast
import importlib.util
import unittest
from pathlib import Path

root = Path(__file__).resolve().parents[1]
source = root / 'engine/tests.py'
module = ast.parse(source.read_text())
names = {'DashboardSetupTemplateTests', 'DashboardReferralTemplateTests', 'DashboardPwaLayoutTemplateTests'}
selected = ast.Module(body=[node for node in module.body if isinstance(node, ast.ClassDef) and node.name in names], type_ignores=[])
# Хелпер грузим по пути файла, без импорта пакета engine (он тянет Django/БД).
helper_spec = importlib.util.spec_from_file_location('engine_test_template_source', root / 'engine/test_template_source.py')
helper = importlib.util.module_from_spec(helper_spec)
helper_spec.loader.exec_module(helper)
namespace = {'SimpleTestCase': unittest.TestCase, 'Path': Path, 'template_source': helper.template_source}
exec(compile(selected, str(source), 'exec'), namespace)
suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(namespace[name]) for name in sorted(names))
raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
