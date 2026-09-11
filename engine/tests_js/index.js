// Точка входа для `node --test engine/tests_js/`: новые версии Node передают
// каталог как модуль, поэтому подключаем здесь все *.test.cjs этого каталога.
const fs = require('node:fs');
const path = require('node:path');

for (const name of fs.readdirSync(__dirname).sort()) {
    if (name.endsWith('.test.cjs')) require(path.join(__dirname, name));
}
