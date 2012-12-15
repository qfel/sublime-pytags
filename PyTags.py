from __future__ import division

import os
import os.path
import re

from operator import eq as op_eq, ne as op_ne

from sublime import ENCODED_POSITION, INHIBIT_EXPLICIT_COMPLETIONS, \
    INHIBIT_WORD_COMPLETIONS, OP_EQUAL, OP_NOT_EQUAL, message_dialog, \
    status_message
from sublime_plugin import EventListener, TextCommand

from pytags.async import async_worker, ui_worker
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


class SymDbClient(LPCClient):
    _databases = None

    def set_databases(self, databases):
        if databases != self._databases or self._process is None:
            self._error = None
            self._databases = databases
            self._call('set_databases', [os.path.expandvars(db['path'])
                                         for db in databases])


# Interface to code running in external Python interpreter. It keeps some state
# in external process (and a tiny cache in Sublime's embedded interpreted too),
# so it is not thread-safe. Use it only in code executed by async_worker.
symdb = SymDbClient(os.path.abspath(os.path.join(
    os.path.dirname(__file__),
    'external',
    'symdb.py')))


class PyTagsCommandMixin(object):
    def is_enabled(self, **kwargs):
        settings = self.view.settings()

        syntax = settings.get('syntax')
        syntax = os.path.splitext(os.path.basename(syntax))[0].lower()
        if syntax != 'python':
            return False

        if not settings.get('pytags_databases'):
            return False

        return True


class PyFindSymbolCommand(PyTagsCommandMixin, TextCommand):
    PROMPT = 'Symbol: '

    def run(self, edit, ask=False):
        # Try to get the symbol from current selection.
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
        def async_search(databases):
            symdb.set_databases(databases)
            results = symdb.query_occurrences(symbol)
            ui_worker.schedule(handle_results, results)

        def handle_results(results):
            if len(results) > 1:
                self.ask_user_result(results)
            elif results:  # len(results) == 1
                self.goto(results[0])
            else:
                message_dialog('Symbol "{0}" not found'.format(symbol))

        async_worker.schedule(async_search,
                              self.view.settings().get('pytags_databases'))

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
    def index_view(self, view):
        databases = view.settings().get('pytags_databases')
        if not databases:
            return

        if view.window():
            project_folders = view.window().folders()
        else:
            # This sometimes happens, no idea when/why.
            project_folders = []

        async_worker.schedule(self.async_index_view, view.file_name(),
                              databases, project_folders)

    @staticmethod
    def async_index_view(file_name, databases, project_folders):
        norm_file_name = os.path.normcase(file_name)
        symdb.set_databases(databases)
        for dbi, database in enumerate(databases):
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

            processed = symdb.process_file(dbi, file_name)

            # process_file may return False due to syntax error, but still
            # update last read time, so commit anyway.
            symdb.commit()

            if processed:
                ui_worker.schedule(status_message, 'Indexed ' + file_name)

    def on_load(self, view):
        file_name = view.file_name()  # This may be None.
        if file_name is not None and is_python_source_file(file_name) and \
                view.settings().get('pytags_index_on_load'):
            self.index_view(view)

    def on_post_save(self, view):
        if is_python_source_file(view.file_name()) and \
                view.settings().get('pytags_index_on_save'):
            self.index_view(view)

    @staticmethod
    def get_prefix(view, pos):
        ''' Return module path prefix overlapping text at pos. '''
        rev_line = view.substr(view.line(pos))[::-1]
        rev_col = len(rev_line) - view.rowcol(pos)[1]
        match = re.match(r'[a-zA-Z0-9_.]*', rev_line[rev_col:])
        return match.group()[::-1]

    def on_query_completions(self, view, prefix, locations):
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
            module_name = get_module_name(view, pos)
            complete_member = True
        elif view.score_selector(pos, 'source.python.pytags.import.module'):
            # One of:
            # from foo.b<ar-to-complete> import..
            # import foo.b<ar-to-complete>
            module_prefix = self.get_prefix(view, locations[0])
            complete_member = False
        else:
            # Not in import/from..import statement context.
            return []

        # Query the database.
        def async_query_completions(databases):
            symdb.set_databases(databases)

            if complete_member:
                members = symdb.query_members(module_name, prefix)
                completions = [(member + '\tMember', member)
                               for member in members]
            else:
                packages = set(package.split('.')[module_prefix.count('.')]
                               for package
                               in symdb.query_packages(module_prefix))
                completions = [(package + '\tModule', package)
                               for package in packages]
            return completions

        result = async_worker.call(async_query_completions,
                                   settings.get('pytags_databases'))
        completions = result.get()
        if settings.get('pytags_exclusive_completions'):
            return (completions,
                    INHIBIT_WORD_COMPLETIONS | INHIBIT_EXPLICIT_COMPLETIONS)
        else:
            return completions

    def on_query_context(self, view, key, operator, operand, match_all):
        if key != 'pytags_index_in_progress':
            return None
        if operator == OP_EQUAL:
            operator = op_eq
        elif operator == OP_NOT_EQUAL:
            operator = op_ne
        else:
            return None
        return operator(PyBuildIndexCommand.index_in_progress, operand)


class PyBuildIndexCommand(PyTagsCommandMixin, TextCommand):
    index_in_progress = False

    def run(self, edit, action='update'):
        if action == 'cancel':
            self.__class__.index_in_progress = False
            return

        if action == 'update':
            rebuild = False
        elif action == 'rebuild':
            rebuild = True
        else:
            raise ValueError('action must be one of {"cancel", "update", '
                             '"rebuild"}')

        self.__class__.index_in_progress = True
        async_worker.schedule(self.async_process_files,
                              self.view.settings().get('pytags_databases', []),
                              self.view.window().folders(), rebuild)

    def is_enabled(self, action='update'):
        if not PyTagsCommandMixin.is_enabled(self):
            return False
        if action == 'cancel':
            return self.index_in_progress
        else:
            return not self.index_in_progress

    @classmethod
    def async_process_files(cls, databases, project_folders, rebuild):
        try:
            cls.async_process_files_inner(databases, project_folders, rebuild)
        finally:
            cls.index_in_progress = False

    @classmethod
    def async_process_files_inner(cls, databases, project_folders, rebuild):
        if rebuild:
            # Helper process should not reference files to be deleted.
            symdb._cleanup()

            # Simply remove associated database files if build from scratch is
            # requested.
            for database in databases:
                try:
                    os.remove(os.path.expandvars(database['path']))
                except OSError:
                    # Specified database file may not yet exist or is
                    # inaccessible.
                    pass

        symdb.set_databases(databases)
        for dbi, database in enumerate(databases):
            roots = database.get('roots', [])
            for i, root in enumerate(roots):
                roots[i] = os.path.expandvars(root)
            if database.get('include_project_folders'):
                roots.extend(project_folders)

            symdb.begin_file_processing(dbi)

            for symbol_root in roots:
                for root, dirs, files in os.walk(symbol_root):
                    for file_name in files:
                        if not cls.index_in_progress:
                            symdb.end_file_processing(dbi)
                            symdb.commit()
                            ui_worker.schedule(status_message,
                                               'Indexing canceled')
                            return
                        if not is_python_source_file(file_name):
                            continue

                        path = os.path.abspath(os.path.join(root,
                                                            file_name))
                        pattern = database.get('pattern')
                        if not pattern or re.search(pattern, path):
                            if symdb.process_file(dbi, path):
                                ui_worker.schedule(status_message,
                                                   'Indexed ' + path)
                                # Do not commit after each file, since it's
                                # very slow.

            symdb.end_file_processing(dbi)
            symdb.commit()

        ui_worker.schedule(status_message, 'Done indexing')
