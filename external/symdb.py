import ast
import os
import os.path
import sqlite3

PERF_DATA_DB = None

if PERF_DATA_DB:
    from time import time


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


class InstrumentedCursor(object):
    ''' A limited SQLite3 cursor implementation that records some query
    execution data into another table.
    '''

    def __init__(self, db):
        self.perfdata_cur = db.cursor()
        self.perfdata_cur.executescript('''
            CREATE TABLE IF NOT EXISTS perfdata (
                query TEXT NOT NULL,
                plan TEXT NOT NULL,
                time REAL NOT NULL
            );
        ''')
        self.cur = db.cursor()

    def execute(self, query, params=None):
        query = query.strip()
        if params is None:
            params = ()
        else:
            params = params,

        self.perfdata_cur.execute('EXPLAIN QUERY PLAN ' + query, *params)
        plan = '\n'.join(row[3] for row in self.perfdata_cur)
        tm = time()
        self.cur.execute(query, *params)
        tm = time() - tm
        self.perfdata_cur.execute('''
            INSERT INTO perfdata(query, plan, time)
            VALUES(:query, :plan, :tm)
        ''', locals())

    def __getattr__(self, name):
        return getattr(self.cur, name)

    def __iter__(self):
        return self.cur


class SymbolDatabase(object):
    has_file_ids = False

    def __init__(self, paths):
        if PERF_DATA_DB:
            self.db = sqlite3.connect(PERF_DATA_DB)
            self.cur = InstrumentedCursor(self.db)
        else:
            self.db = sqlite3.connect(':memory:')
            self.cur = self.db.cursor()

        # Load specified databases.
        for dbi, path in enumerate(paths):
            self.cur.execute('''
                ATTACH DATABASE ? AS ?
            ''', (path, 'db{0}'.format(dbi)))
            self.try_create_schema(dbi)

        # Create views of all files and symbols defined in specified databases.
        self.cur.execute('CREATE TEMP VIEW all_symbols AS ' +
            ' UNION ALL '.join(
                'SELECT *, {0} AS dbid FROM db{0}.symbols'.format(dbi)
                for dbi in xrange(len(paths))))
        self.cur.execute('CREATE TEMP VIEW all_files AS ' +
            ' UNION ALL '.join(
                'SELECT *, {0} AS dbid FROM db{0}.files'.format(dbi)
                for dbi in xrange(len(paths))))

    def try_create_schema(self, dbi):
        self.cur.executescript('''
            CREATE TABLE IF NOT EXISTS db{0}.symbols (
                file_id INTEGER REFERENCES files(id),
                symbol TEXT NOT NULL,  -- Symbol name, valid Python identifier.
                scope TEXT NOT NULL,   -- Scope inside a file (eg. class name).
                row INTEGER NOT NULL,
                col INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS db{0}.symbols_symbol ON symbols(symbol);

            CREATE TABLE IF NOT EXISTS db{0}.files (
                id INTEGER PRIMARY KEY,
                path TEXT NOT NULL UNIQUE,
                package TEXT NOT NULL,     -- Package name (eg. "os.path").
                timestamp REAL NOT NULL    -- Last modification time.
            );
            CREATE UNIQUE INDEX IF NOT EXISTS db{0}.files_path ON files(path);
            CREATE INDEX IF NOT EXISTS db{0}.files_package ON files(package);
        '''.format(dbi))

    def add(self, dbi, symbol, scope, path, row, col):
        self.cur.execute('''
            INSERT INTO db{0}.symbols(file_id, symbol, scope, row, col)
            VALUES(
                (SELECT id FROM db{0}.files WHERE path = :path),
                :symbol, :scope, :row, :col
            )
        '''.format(dbi), locals())

    def clear_file(self, dbi, name):
        self.cur.execute('''
            DELETE FROM db{0}.symbols WHERE
                file_id = (SELECT id FROM db{0}.files WHERE path = :name)
        '''.format(dbi), locals())

    def begin_file_processing(self, dbi):
        self.has_file_ids = True
        self.cur.execute('DROP TABLE IF EXISTS file_ids')
        self.cur.execute('''
            CREATE TEMP TABLE file_ids (
                file_id INTEGER NOT NULL
            )
        ''')

    def end_file_processing(self, dbi):
        self.cur.execute('''
            DELETE FROM db{0}.symbols WHERE file_id NOT IN (
                SELECT file_id FROM file_ids)
        '''.format(dbi))
        self.cur.execute('''
            DELETE FROM db{0}.files WHERE id NOT IN (
                SELECT file_id FROM file_ids)
        '''.format(dbi))
        self.cur.execute('DROP TABLE file_ids')
        self.has_file_ids = False

    def update_file_time(self, dbi, path, time):
        self.cur.execute('''
            SELECT id, timestamp FROM db{0}.files WHERE path = :path
        '''.format(dbi), locals())
        try:
            file_id, timestamp = self.cur.fetchone()
        except TypeError:
            package = get_package(path)
            self.cur.execute('''
                INSERT INTO db{0}.files(path, package, timestamp)
                VALUES(:path, :package, :time)
            '''.format(dbi), locals())
            file_id = self.cur.lastrowid
            result = True
        else:
            if timestamp < time:
                package = get_package(path)
                self.cur.execute('''
                    UPDATE db{0}.files
                    SET timestamp = :time, package = :package
                    WHERE id = :file_id
                '''.format(dbi), locals())
                result = True
            else:
                result = False

        if self.has_file_ids:
            self.cur.execute('INSERT INTO file_ids VALUES(:file_id)', locals())

        return result

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
                '.' || f.package || '.' || s.scope GLOB :namespace
            ORDER BY s.symbol, f.path, s.row
        ''', locals())
        for row in self.cur:
            yield self._result_row_to_dict(row)

    def members(self, package, prefix):
        self.cur.execute('''
            SELECT DISTINCT s.symbol
            FROM all_symbols s, all_files f
            WHERE
                s.file_id = f.id AND
                s.dbid = f.dbid AND
                f.package = :package AND
                s.symbol GLOB :prefix || '*' AND
                s.scope = ''
            ORDER BY s.symbol, f.path, s.row
        ''', locals())
        return (row[0] for row in self.cur)

    def packages(self, prefix):
        self.cur.execute('''
            SELECT DISTINCT package
            FROM all_files
            WHERE package GLOB :prefix || '*'
        ''', locals())
        return (row[0] for row in self.cur)


class SymbolExtractor(ast.NodeVisitor):
    def __init__(self, db, dbi, path):
        self.path = path
        self.db = db
        self.dbi = dbi
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
        self.db.add(self.dbi, name, '.'.join(self.scope), self.path,
                    node.lineno - 1, node.col_offset)


# Functions served as LPCs.

db = None


def set_databases(paths):
    global db
    db = SymbolDatabase(paths)


def begin_file_processing(dbi):
    db.begin_file_processing(dbi)


def end_file_processing(dbi):
    db.end_file_processing(dbi)


def process_file(dbi, path):
    path = os.path.normcase(os.path.normpath(path))
    if db.update_file_time(dbi, path, os.path.getmtime(path)):
        db.clear_file(dbi, path)
        try:
            file_ast = ast.parse(open(path).read(), path)
        except:
            return False
        SymbolExtractor(db, dbi, path).visit(file_ast)
        return True
    else:
        return False


def query_occurrences(symbol):
    return list(db.occurrences(symbol))


def query_members(package, prefix):
    return list(db.members(package, prefix))


def query_packages(prefix):
    return list(db.packages(prefix))


def commit():
    db.commit()
