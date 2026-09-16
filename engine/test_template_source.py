"""Read both a template and its extracted assets for source-level guards."""
import re
from pathlib import Path


def template_source(path):
    source = Path(path).read_text()
    # {% include "includes/x.html" %} — часть той же страницы (мобильный кабинет).
    includes = re.findall(r"{%\s*include\s+['\"]([^'\"]+)['\"]\s*%}", source)
    for name in includes:
        include_path = Path('engine/templates') / name
        if include_path.exists():
            source += '\n' + include_path.read_text()
    assets = re.findall(r"{%\s*static\s+['\"]((?:styles|scripts|js)/[^'\"]+)['\"]\s*%}", source)
    return source + '\n' + '\n'.join((Path('engine/static') / name).read_text() for name in assets)
