<?php

// Парсер Python-файлов для buildGraph.php (регулярні вирази + стек відступів, не AST).
// Використовує insertSymbol() і insertRef() із buildGraph.php.
//
// Символи: class, method (Class::method), function (у т. ч. вкладені: outer.inner),
//          property (константи UPPER_CASE модуля/класу), route (FastAPI: "GET /path").
// Зв'язки: import, call, instantiate (виклик Name(...)), extends, decorator.

const PY_SKIP_CALLS = [
    'if', 'elif', 'while', 'for', 'return', 'yield', 'await', 'assert', 'with', 'not',
    'and', 'or', 'in', 'is', 'lambda', 'del', 'raise', 'except', 'print', 'len', 'str',
    'int', 'float', 'bool', 'list', 'dict', 'set', 'tuple', 'range', 'isinstance',
    'super', 'min', 'max', 'sum', 'abs', 'round', 'sorted', 'enumerate', 'zip', 'any',
    'all', 'getattr', 'setattr', 'hasattr', 'type', 'repr', 'map', 'filter', 'iter',
    'next', 'open', 'format', 'bytes', 'object', 'id', 'hash', 'reversed', 'callable',
];

// Прибирає вміст рядків і коментарі; $triple зберігає стан багаторядкового рядка.
function pyStripLine(string $line, ?string &$triple): string {
    $out = '';
    $len = strlen($line);
    $i   = 0;
    if ($triple !== null) {
        $end = strpos($line, $triple);
        if ($end === false) return '';
        $i = $end + 3;
        $triple = null;
        $out .= '""';
    }
    while ($i < $len) {
        $c = $line[$i];
        if ($c === '#') break;
        if ($c === '"' || $c === "'") {
            $q3 = substr($line, $i, 3);
            if ($q3 === '"""' || $q3 === "'''") {
                $end = strpos($line, $q3, $i + 3);
                if ($end === false) { $triple = $q3; $out .= '""'; return $out; }
                $out .= '""';
                $i = $end + 3;
                continue;
            }
            $j = $i + 1;
            while ($j < $len && $line[$j] !== $c) $j += ($line[$j] === '\\') ? 2 : 1;
            $out .= '""';
            $i = $j + 1;
            continue;
        }
        $out .= $c;
        $i++;
    }
    return $out;
}

function pyVisibility(string $name): string {
    if (preg_match('/^__\w+__$/', $name)) return 'public';
    if (str_starts_with($name, '__'))      return 'private';
    if (str_starts_with($name, '_'))       return 'protected';
    return 'public';
}

function pyImportNames(string $list): array {
    $names = [];
    foreach (explode(',', str_replace(['(', ')', '\\'], '', $list)) as $part) {
        $part = trim($part);
        if ($part === '' || $part === '*') continue;
        $names[] = preg_split('/\s+as\s+/', $part)[0];
    }
    return $names;
}

function parsePython(PDO $db, int $fileId, string $content, string $relPath): void {
    $lines   = explode("\n", $content);
    $triple  = null;
    $depth   = 0;       // баланс дужок: >0 — продовження логічного рядка
    $stack   = [];      // [indent, kind(class|def), fullName, className]
    $pendingStatic = 0; // @staticmethod / @classmethod перед def
    $pendingRoute  = null; // HTTP-метод декоратора без шляху в тому ж рядку
    $routes  = [];      // маршрути, що чекають на def
    $importFrom = null; // модуль багаторядкового from X import (...)

    foreach ($lines as $i => $raw) {
        $lineNum  = $i + 1;
        $wasTriple = $triple !== null;
        $clean    = pyStripLine(rtrim($raw, "\r"), $triple);
        $line     = trim($clean);
        $continuation = $depth > 0 || $wasTriple;
        $depth    = max(0, $depth + preg_match_all('/[\(\[\{]/', $clean) - preg_match_all('/[\)\]\}]/', $clean));

        // Шлях маршруту з наступного рядка багаторядкового декоратора
        if ($pendingRoute !== null && preg_match('/["\']([^"\']*)["\']/', $raw, $m)) {
            $routes[] = $pendingRoute . ' ' . $m[1];
            $pendingRoute = null;
        }
        if ($line === '') continue;

        // Продовження багаторядкового from X import (...)
        if ($importFrom !== null) {
            foreach (pyImportNames($line) as $n)
                insertRef($db, $fileId, $relPath, $n, 'import', $lineNum);
            if ($depth === 0) $importFrom = null;
            continue;
        }

        // Новий логічний рядок: закрити області з більшим або рівним відступом
        if (!$continuation) {
            $indent = strlen(str_replace("\t", '    ', $raw)) - strlen(ltrim(str_replace("\t", '    ', $raw)));
            while ($stack && end($stack)[0] >= $indent) array_pop($stack);
        } else {
            $indent = $stack ? end($stack)[0] + 1 : 0;
        }

        $top       = $stack ? end($stack) : null;
        $context   = $top ? $top[2] : $relPath;
        $className = $top ? $top[3] : '';

        // import
        if (!$continuation && preg_match('/^from\s+([\w.]+)\s+import\s+(.+)$/', $line, $m)) {
            insertRef($db, $fileId, $context, $m[1], 'import', $lineNum);
            foreach (pyImportNames($m[2]) as $n)
                insertRef($db, $fileId, $context, $n, 'import', $lineNum);
            if ($depth > 0) $importFrom = $m[1];
            continue;
        }
        if (!$continuation && preg_match('/^import\s+(.+)$/', $line, $m)) {
            foreach (pyImportNames($m[1]) as $n) {
                insertRef($db, $fileId, $context, $n, 'import', $lineNum);
                $parts = explode('.', $n);
                if (count($parts) > 1) insertRef($db, $fileId, $context, end($parts), 'import', $lineNum);
            }
            continue;
        }

        // декоратори
        if (!$continuation && preg_match('/^@([\w.]+)/', $line, $m)) {
            $deco = $m[1];
            if (in_array($deco, ['staticmethod', 'classmethod'], true)) { $pendingStatic = 1; continue; }
            insertRef($db, $fileId, $context, $deco, 'decorator', $lineNum);
            if (preg_match('/^@\w+\.(get|post|put|delete|patch|head|options|websocket|api_route)\(/', $line, $rm)) {
                $method = strtoupper($rm[1]);
                if (preg_match('/^\s*@[\w.]+\(\s*["\']([^"\']*)["\']/', $raw, $pm)) $routes[] = "$method {$pm[1]}";
                else $pendingRoute = $method;
            }
            continue;
        }

        // class
        if (!$continuation && preg_match('/^class\s+(\w+)\s*(?:\((.*?)\)?)?\s*:?$/', $line, $m)) {
            $name     = $m[1];
            $fullName = $className ? "{$className}.{$name}" : $name;
            insertSymbol($db, $fileId, 'class', $name, $fullName, $lineNum, pyVisibility($name));
            foreach (explode(',', $m[2] ?? '') as $base) {
                $base = trim($base);
                if ($base === '' || str_contains($base, '=')) continue;
                $parts = explode('.', $base);
                insertRef($db, $fileId, $fullName, end($parts), 'extends', $lineNum);
            }
            $stack[] = [$indent, 'class', $fullName, $fullName];
            $pendingStatic = 0; $routes = [];
            continue;
        }

        // def / async def
        if (!$continuation && preg_match('/^(?:async\s+)?def\s+(\w+)\s*\(/', $line, $m)) {
            $name = $m[1];
            if ($top && $top[1] === 'class') {
                $type = 'method'; $fullName = "{$top[2]}::{$name}";
            } elseif ($top) {
                $type = 'function'; $fullName = "{$top[2]}.{$name}";
            } else {
                $type = 'function'; $fullName = $name;
            }
            insertSymbol($db, $fileId, $type, $name, $fullName, $lineNum, pyVisibility($name), $pendingStatic);
            foreach ($routes as $route)
                insertSymbol($db, $fileId, 'route', substr($route, strpos($route, ' ') + 1), $route, $lineNum, 'public');
            $stack[] = [$indent, 'def', $fullName, $className];
            $pendingStatic = 0; $routes = [];
            $context = $fullName;
            $line = substr($line, strpos($line, '(')); // параметри/значення за замовчуванням
        }

        // константи UPPER_CASE рівня модуля або класу
        if (!$continuation && (!$top || $top[1] === 'class')
            && preg_match('/^([A-Z][A-Z0-9_]*)\s*(?::[^=]+)?=(?!=)/', $line, $m)) {
            $fullName = $top ? "{$top[2]}::{$m[1]}" : $m[1];
            insertSymbol($db, $fileId, 'property', $m[1], $fullName, $lineNum, 'public');
        }

        // виклики методів: obj.name(
        if (preg_match_all('/\.(\w+)\s*\(/', $line, $mm)) {
            foreach ($mm[1] as $fn)
                insertRef($db, $fileId, $context, $fn, 'call', $lineNum);
        }
        // прямі виклики: name( / Name(
        if (preg_match_all('/(?<![\w.])([A-Za-z_]\w*)\s*\(/', $line, $mm)) {
            foreach ($mm[1] as $fn) {
                if (in_array($fn, PY_SKIP_CALLS, true)) continue;
                $refType = ctype_upper($fn[0]) ? 'instantiate' : 'call';
                insertRef($db, $fileId, $context, $fn, $refType, $lineNum);
            }
        }
    }
}
