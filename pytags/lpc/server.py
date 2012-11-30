import cPickle as pickle

from imp import load_source
from sys import argv, stdin, stdout


# This module is handles data pickled by different Python version, so use
# explicit protocol number.
PICKLE_PROTOCOL = 2


def main():
    if len(argv) != 2:
        raise ValueError('Need exactly 1 argument (module)')

    module = load_source('external_module', argv[1])

    while True:
        try:
            cmd = pickle.load(stdin)
        except EOFError:
            break
        handler = getattr(module, cmd[0])
        pickle.dump(handler(*cmd[1], **cmd[2]), stdout, PICKLE_PROTOCOL)


if __name__ == '__main__':
    main()
