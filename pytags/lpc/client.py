import cPickle as pickle
import os
import os.path

from subprocess import PIPE, Popen
from time import time


# For debugging. Set to True from Python console to enable logging all LPC
# calls to stdout.
LOG_LPC = False

# This module is handles data pickled by different Python version, so use
# explicit protocol number.
PICKLE_PROTOCOL = 2

# Path to LPC server implementation. This must be computed here, as __file__ is
# relative and valid only when this script is being loaded.
SERVER_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                           'server.py'))


class LPCError(Exception):
    pass


class FunctionProxy(object):
    def __init__(self, client, name):
        self.client = client
        self.name = name

    def __call__(self, *args, **kwargs):
        return self.client._call(self.name, *args, **kwargs)


class LPCClient(object):
    _process = None
    _error = None

    def __init__(self, module, python='python'):
        self._args = [python, '-u', SERVER_PATH, module]

    def _startup(self):
        if self._error is not None:
            raise LPCError('Did not recover from previous error')
        elif self._process is None:
            # Popen leaks file descriptors (at least on Linux, see
            # http://bugs.python.org/issue7213). This has an impact when ST2
            # reloads the plugin: LPClient instances get orphaned on reload,
            # but due to pipe leak the other end does not get EOFError. If
            # you're playing with this plugin's code keep in mind each reload
            # may cost you one zombie-like process. On Linux you can temporary
            # use close_fds=True (it's not passed by default for performance
            # reasons).
            self._process = Popen(args=self._args, stdin=PIPE, stdout=PIPE,
                                  stderr=PIPE)

    def _cleanup(self, error=None):
        if self._process is None:
            if error is not None:
                raise ValueError('Cannot do error-cleanup when not '
                                 'initialized')
            return

        self._process.stdin.close()
        self._process.stdout.close()
        self._process.stderr.close()
        self._process.wait()
        self._process = None
        self._error = error

    def _call(self, _name, *args, **kwargs):
        self._startup()

        if LOG_LPC:
            args = list(args)
            args.extend('{0}={1!r}'.format(k, v)
                        for k, v in kwargs.iteritems())
            print '[LPC] {0}({1})'.format(_name,
                                          ', '.join(repr(a) for a in args)),
            begin_time = time()

        try:
            pickle.dump((_name, args, kwargs), self._process.stdin,
                        PICKLE_PROTOCOL)
            ret = pickle.load(self._process.stdout)

            if LOG_LPC:
                print '= {0!r} ({1:.3f}s)'.format(ret, time() - begin_time)

            return ret
        except (IOError, EOFError, pickle.UnpicklingError) as e:
            if LOG_LPC:
                print '! {0}'.format(e)

            e = LPCError(self._process.stderr.read())
            self._cleanup(e)
            raise e

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)
        proxy = FunctionProxy(self, name)
        setattr(self, name, proxy)
        return proxy
