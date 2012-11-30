import ast
import os
import os.path

from sqlite3 import connect as sqlite_connect


class SymbolDatabase(object):
    def __init__(self, path, others):
        self.db = sqlite_connect(path)
        self.cur = self.db.cursor()
        self.cur.executescript('''
            CREATE TABLE IF NOT EXISTS symbols (
                file_id INTEGER REFERENCES files(id),
                symbol TEXT NOT NULL,  -- Symbol name, valid Python identifier.
                scope TEXT NOT NULL,   -- Scope inside a file (eg. class name).
                row INTEGER NOT NULL,
                col INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS symbols_symbol ON symbols(symbol);

            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY,
                path TEXT NOT NULL UNIQUE,
                package TEXT NOT NULL,     -- Package name (eg. "os.path").
                timestamp REAL NOT NULL    -- Last modification time.
            );
            CREATE UNIQUE INDEX IF NOT EXISTS files_path ON files(path);
            CREATE INDEX IF NOT EXISTS files_package ON files(package);
        ''')

        self.db_prefixes = ['']
        for i in xrange(len(others)):
            db_name = 'db{}'.format(i)
            self.cur.execute('''
                ATTACH DATABASE ? AS ?
            ''', (others[i], db_name))
            self.db_prefixes.append('{}.'.format(db_name))

        # Performance sucks when using views.
        self.cur.execute('CREATE TEMP VIEW all_symbols AS ' +
            ' UNION ALL '.join(
                'SELECT *, {0} AS dbid FROM {1}symbols'.format(i, prefix)
                for i, prefix in enumerate(self.db_prefixes)))
        self.cur.execute('CREATE TEMP VIEW all_files AS ' +
            ' UNION ALL '.join(
                'SELECT *, {0} AS dbid FROM {1}files'.format(i, prefix)
                for i, prefix in enumerate(self.db_prefixes)))

    def add(self, symbol, scope, path, row, col):
        self.cur.execute('''
            INSERT INTO symbols(file_id, symbol, scope, row, col)
            VALUES(
                (SELECT id FROM files WHERE path = :path),
                :symbol, :scope, :row, :col
            )
        ''', locals())

    def clear_file(self, name):
        self.cur.execute('''
            DELETE FROM symbols WHERE
                file_id = (SELECT id FROM files WHERE path = :name)
        ''', locals())

    def remove_other_files(self, file_paths):
        self.cur.execute('''
            CREATE TEMP TABLE file_ids (
                file_id INTEGER REFERENCES files(id)
            )
        ''')
        for file_path in file_paths:
            self.cur.execute('''
                INSERT INTO file_ids
                SELECT id
                FROM files WHERE path = :file_path
            ''', {'file_path': file_path})
        self.cur.execute('''
            DELETE FROM symbols WHERE file_id NOT IN (
                SELECT file_id FROM file_ids)
        ''')
        self.cur.execute('''
            DELETE FROM files WHERE id NOT IN (
                SELECT file_id FROM file_ids)
        ''')

    def update_file_time(self, path, time):
        self.cur.execute('''
            SELECT timestamp FROM files WHERE path = :path
        ''', locals())
        row = self.cur.fetchone()
        if row:
            if row[0] < time:
                package = get_package(path)
                self.cur.execute('''
                    UPDATE files
                    SET timestamp = :time, package = :package
                    WHERE path = :path
                ''', locals())
                return True
            else:
                return False
        else:
            package = get_package(path)
            self.cur.execute('''
                INSERT INTO files(path, package, timestamp)
                VALUES(:path, :package, :time)
            ''', locals())
            return True

    def commit(self):
        self.db.commit()

    def _result_row_to_dict(self, row):
        return {
            'symbol': row[0],
            'scope': row[1],
            'package': row[2],
            'row': row[3],
            'col': row[4],
            'file': row[5]
        }

    def occurrences(self, symbol):
        namespace, sep, symbol = symbol.rpartition('.')
        if sep:
            namespace = '*.' + namespace
        else:
            namespace = '*'

        self.cur.execute('''
            SELECT s.symbol, s.scope, f.package, s.row, s.col, f.path
            FROM all_symbols s, all_files f
            WHERE
                s.file_id = f.id AND
                s.dbid = f.dbid AND
                s.symbol = :symbol AND
                GLOB(:namespace, '.' || f.package || '.' || s.scope)
            ORDER BY s.symbol, f.path, s.row
        ''', locals())
        for row in self.cur:
            yield self._result_row_to_dict(row)

    def query_packages(self, pattern):
        self.cur.execute('''
            SELECT DISTINCT package FROM all_files WHERE package GLOB :pattern
        ''', locals())
        return (row[0] for row in self.cur)


class SymbolExtractor(ast.NodeVisitor):
    def __init__(self, db, path):
        self.path = path
        self.db = db
        self.scope = []
        self.this = None

    def generic_visit(self, node):
        if not isinstance(node, ast.expr):
            ast.NodeVisitor.generic_visit(self, node)

    def visit_FunctionDef(self, node):
        self.add_symbol(node.name, node)

    def visit_ClassDef(self, node):
        if self.this is None:
            self.add_symbol(node.name, node)
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

    def visit_Assign(self, node):
        self.process_assign(node.targets)

    def process_assign(self, targets):
        for target in targets:
            if isinstance(target, (ast.Tuple, ast.List)):
                self.process_assign(target.elts)
            elif isinstance(target, ast.Attribute):
                if isinstance(target.value, ast.Name) and \
                        target.value.id == self.this:
                    self.add_symbol(target.attr, target)
            elif isinstance(target, ast.Name) and self.this is None:
                self.add_symbol(target.id, target)

    def add_symbol(self, name, node):
        self.db.add(name, '.'.join(self.scope), self.path, node.lineno - 1,
                    node.col_offset)


db = None


def set_db(paths):
    global db
    db = SymbolDatabase(paths[0], paths[1:])


def get_package(path):
    assert path.endswith('.py')
    path = path[:-3]
    path, module = os.path.split(path)
    if module == '__init__':
        package = []
    else:
        package = [module]
    while True:
        new_path, module = os.path.split(path)
        if not os.path.isfile(os.path.join(path, '__init__.py')):
            break
        package.append(module)
        if new_path == path:
            break
        path = new_path
    return '.'.join(reversed(package))


def process_file(path, force=False):
    path = os.path.normcase(os.path.normpath(path))
    if db.update_file_time(path, os.path.getmtime(path)) or force:
        db.clear_file(path)
        try:
            file_ast = ast.parse(open(path).read(), path)
        except:
            return False
        SymbolExtractor(db, path).visit(file_ast)
        return True
    else:
        return False


def remove_other_files(file_paths):
    db.remove_other_files(file_paths)


def query_occurrences(symbol):
    return list(db.occurrences(symbol))


def query_packages(pattern):
    return list(db.query_packages(pattern))


def commit():
    db.commit()
