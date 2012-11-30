import cPickle as pickle
import os
import os.path

from subprocess import PIPE, Popen


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
        process = self.client._process
        try:
            pickle.dump((self.name, args, kwargs), process.stdin,
                        PICKLE_PROTOCOL)
            return pickle.load(process.stdout)
        except (IOError, EOFError, pickle.UnpicklingError):
            raise LPCError(process.stderr.read())


class LPCClient(object):
    def __init__(self, module, python='python'):
        self._process = Popen(args=[python, '-u', SERVER_PATH, module],
                              stdin=PIPE, stdout=PIPE, stderr=PIPE)

    def _cleanup(self):
        self._process.stdin.close()
        self._process.wait()

    def __del__(self):
        self._cleanup()

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)
        proxy = FunctionProxy(self, name)
        setattr(self, name, proxy)
        return proxy

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._cleanup()
        return False
