from __future__ import division

import os
import os.path
import re

from functools import partial

from sublime import ENCODED_POSITION, INHIBIT_EXPLICIT_COMPLETIONS, \
    INHIBIT_WORD_COMPLETIONS, message_dialog, set_timeout, status_message
from sublime_plugin import EventListener, TextCommand

from pytags.async import async_worker
from pytags.lpc.client import LPCClient


def get_module_name(view, pos):
    while True:
         # Should not happen if syntax definition works correctly.
        assert pos >= 0

        scope = view.extract_scope(pos)
        if view.score_selector(scope.a, 'source.python.pytags.import.module'):
            return view.substr(scope).strip()
        pos = scope.a - 1


def is_python_source_file(file_name):
    return file_name.endswith('.py') or file_name.endswith('.pyw')


def async_status_message(msg):
    set_timeout(partial(status_message, msg), 0)


def get_ordered_databases(settings):
    databases = settings.get('pytags_databases', [])
    databases.sort(key=lambda db: db.get('index', float('inf')))
    return databases


SymDb = partial(LPCClient,
                os.path.abspath(os.path.join(os.path.dirname(__file__),
                                             'external', 'symdb.py')))


class PythonCommandMixin(object):
    def is_enabled(self):
        syntax = self.view.settings().get('syntax')
        syntax = os.path.splitext(os.path.basename(syntax))[0].lower()
        return syntax == 'python'


class PyFindSymbolCommand(PythonCommandMixin, TextCommand):
    PROMPT = 'Symbol: '

    def run(self, edit, ask=False):
        sel = self.view.sel()
        if len(sel) == 1:
            sel = sel[0]
            if sel.empty():
                sel = self.view.word(sel)
            symbol = self.view.substr(sel).strip()
        else:
            symbol = ''

        if ask or not symbol:
            self.ask_user_symbol(symbol)
        else:
            self.search(symbol)

    def ask_user_symbol(self, symbol):
        self.view.window().show_input_panel(self.PROMPT, symbol, self.search,
                                            None, None)

    def search(self, symbol):
        databases = get_ordered_databases(self.view.settings())
        with SymDb() as symdb:
            symdb.set_db([os.path.expandvars(db['path']) for db in databases])
            results = symdb.query_occurrences(symbol)

        if len(results) > 1:
            self.ask_user_result(results)
        elif results:  # len(results) == 1
            self.goto(results[0])
        else:
            message_dialog('Symbol "{0}" not found'.format(symbol))

    def ask_user_result(self, results):
        def on_select(i):
            if i != -1:
                self.goto(results[i])

        self.view.window().show_quick_panel(map(self.format_result, results),
                                            on_select)

    def goto(self, result):
        self.view.window().open_file('{0}:{1}:{2}'.format(result['file'],
                                                          result['row'] + 1,
                                                          result['col'] + 1),
                                     ENCODED_POSITION)

    def format_result(self, result):
        dir_name, file_name = os.path.split(result['file'])
        return ['.'.join(filter(None, (result['package'], result['scope'],
                                       result['symbol']))),
                u'{0}:{1}'.format(file_name, result['row']),
                dir_name]


class PyTagsListener(EventListener):
    @classmethod
    def index_view(cls, view):
        file_name = view.file_name()
        norm_file_name = os.path.normcase(file_name)
        if view.window():
            project_folders = view.window().folders()
        else:
            project_folders = []
        for database in get_ordered_databases(view.settings()):
            roots = database.get('roots', [])
            if database.get('include_project_folders'):
                roots.extend(project_folders)
            if roots:
                for root in roots:
                    root = os.path.normcase(
                        os.path.normpath(os.path.expandvars(root)))
                    if norm_file_name.startswith(root + os.sep):
                        break
                else:
                    continue

            pattern = database.get('pattern')
            if pattern and not re.search(pattern, file_name):
                continue

            async_worker.execute(cls.async_process_file, file_name,
                                 database['path'])

    @staticmethod
    def async_process_file(file_name, database_path):
        with SymDb() as symdb:
            symdb.set_db([os.path.expandvars(database_path)])
            if symdb.process_file(file_name):
                async_status_message('Indexed ' + file_name)
            symdb.commit()

    @classmethod
    def on_load(cls, view):
        file_name = view.file_name()  # This may be None.
        if file_name is not None and is_python_source_file(file_name) and \
                view.settings().get('pytags_index_on_load'):
            cls.index_view(view)

    @classmethod
    def on_post_save(cls, view):
        if is_python_source_file(view.file_name()):
            if view.settings().get('pytags_index_on_save'):
                cls.index_view(view)

    @staticmethod
    def get_prefix(view, pos):
        ''' Return module path prefix overlapping text at pos. '''
        rev_line = view.substr(view.line(pos))[::-1]
        rev_col = len(rev_line) - view.rowcol(pos)[1]
        match = re.match(r'[a-zA-Z0-9_.]*', rev_line[rev_col:])
        return match.group()[::-1]

    @classmethod
    def on_query_completions(cls, view, prefix, locations):
        settings = view.settings()

        # Test if completion disabled by user.
        if not settings.get('pytags_complete_imports'):
            return []

        # Automatically use completion-enabled syntax.
        if settings.get('syntax') == 'Packages/Python/Python.tmLanguage':
            view.set_syntax_file('Packages/PyTags/Python.tmLanguage')

        # Test for single selection (multiple selections are unsupported).
        if len(locations) != 1:
            return []
        pos = locations[0]

        # Defined scope names do not catch newlines, but the user will often
        # place cursor at the end of the line.
        if view.substr(pos) == '\n':
            pos -= 1

        # Test for import context.
        if view.score_selector(pos, 'source.python.pytags.import.member'):
            # from..import foo.b<ar-to-complete>
            complete_member = True
        elif view.score_selector(pos, 'source.python.pytags.import.module'):
            # One of:
            # from foo.b<ar-to-complete> import..
            # import foo.b<ar-to-complete>
            complete_member = False
        else:
            # Not in import/from..import statement context.
            return []

        # Query the database.
        with SymDb() as symdb:
            symdb.set_db([os.path.expandvars(db['path'])
                          for db in settings.get('pytags_databases')])

            if complete_member:
                members = symdb.query_members(get_module_name(view, pos),
                                              prefix)
                completions = [(member + '\tMember', member)
                               for member in members]
            else:
                prefix = cls.get_prefix(view, locations[0])
                packages = set(package.split('.')[prefix.count('.')]
                               for package
                               in symdb.query_packages(prefix + '*'))
                completions = [(package + '\tModule', package)
                               for package in packages]

        if settings.get('pytags_exclusive_completions'):
            return (completions,
                    INHIBIT_WORD_COMPLETIONS | INHIBIT_EXPLICIT_COMPLETIONS)
        else:
            return completions


class PyBuildIndexCommand(PythonCommandMixin, TextCommand):
    def run(self, edit, rebuild=False):
        async_worker.execute(self.async_process_files,
                             self.view.settings().get('pytags_databases', []),
                             self.view.window().folders(), rebuild)

    @staticmethod
    def async_process_files(databases, project_folders, rebuild):
        with SymDb() as symdb:
            for database in databases:
                if rebuild:
                    try:
                        os.remove(database['path'])
                    except OSError:
                        pass

                roots = database.get('roots', [])
                for i, root in enumerate(roots):
                    roots[i] = os.path.expandvars(root)
                if database.get('include_project_folders'):
                    roots.extend(project_folders)

                symdb.set_db([os.path.expandvars(database['path'])])
                paths = []
                for symbol_root in roots:
                    for root, dirs, files in os.walk(symbol_root):
                        for file_name in files:
                            if is_python_source_file(file_name):
                                path = os.path.abspath(os.path.join(root,
                                                                    file_name))
                                pattern = database.get('pattern')
                                if not pattern or re.search(pattern, path):
                                    paths.append(path)
                                    if symdb.process_file(path, rebuild):
                                        async_status_message('Indexed ' + path)

                symdb.remove_other_files(paths)
                symdb.commit()

        async_status_message('Done indexing')
