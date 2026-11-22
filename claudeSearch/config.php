<?php

// Radio 345: локальний пошук коду, без доступу до робочих даних.
$rootDir = realpath(__DIR__ . '/..') . DIRECTORY_SEPARATOR;

// У проєкті SQLite events.db, а не MySQL. Не під'єднувати її як граф коду.
// Вбудовані schema/db використовують MySQL і для цього репозиторію вимкнені.
define('CS_DB_HOST', '');
define('CS_DB_NAME', 'events.db');
define('CS_DB_USER', '');
define('CS_DB_PASS', '');
if (in_array($action ?? null, ['schema', 'db'], true)) {
    fwrite(STDERR, "Radio 345 використовує SQLite; команди MySQL schema/db недоступні.\n");
    exit(2);
}

// Окремий автоматично створюваний індекс, не база подій Radio 345.
$dbPath = __DIR__ . DIRECTORY_SEPARATOR . 'code_graph.sqlite';

// Структурний граф: JavaScript панелі, Mini App, інструментів і тестів
// та Python усього репозиторію (parsers/python.php; .claudeignore виключає .venv тощо).
$scanDirs = [
    'js' => [
        $rootDir . 'local_admin',
        $rootDir . 'miniapp',
        $rootDir . 'tools',
        $rootDir . 'tests',
    ],
    'py' => [$rootDir],
];
$scanExts = ['js' => ['js', 'cjs', 'mjs'], 'py' => ['py']];

// Корінь включає також radio.py, config.py та CLI; ignore.php читає .claudeignore.
// Один корінь запобігає повторному рекурсивному обходу вкладених каталогів.
$searchDirs = [$rootDir];
$extensions = [
    'py', 'js', 'cjs', 'mjs', 'html', 'css', 'md',
    'json', 'toml', 'yaml', 'yml', 'ini', 'cfg', 'sh', 'txt', 'sql',
];

// SQL у цьому проєкті міститься переважно у Python, зокрема в services/ та api/.
$sqlDirs = [$rootDir];
$sqlExtensions = ['py', 'sql'];

// PHP action route не розуміє декоратори FastAPI; шукати через raw/usages.
$routeFile = $rootDir . 'api/app.py';
if (($action ?? null) === 'route') {
    fwrite(STDERR, "Маршрути FastAPI: використайте raw або usages для пошуку в api/.\n");
    exit(2);
}

// Embeddings вимкнені: код не надсилається зовнішнім провайдерам.
