// Скрипт локальної панелі так, як його віддає сервер (server/api/local_dashboard.py → panel_script).
const fs = require('node:fs');
const path = require('node:path');
const dir = path.join(__dirname, '../local_admin/js');
const parts = fs.readdirSync(dir).filter(name => name.endsWith('.js')).sort()
  .map(name => fs.readFileSync(path.join(dir, name), 'utf8')).join('');
module.exports = '"use strict";\n// Radio 345 · локальна панель. Лише 127.0.0.1; дані з того самого Mac.\n(() => {\n' + parts + '})();\n';
