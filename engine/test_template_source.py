"""Read both a template and its extracted assets for source-level guards."""
import re
from pathlib import Path


def template_source(path):
    source = Path(path).read_text()
    assets = re.findall(r"{%\s*static\s+['\"]((?:styles|scripts)/[^'\"]+)['\"]\s*%}", source)
    return source + '\n' + '\n'.join((Path('engine/static') / name).read_text() for name in assets)
