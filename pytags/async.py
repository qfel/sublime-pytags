import os
import os.path

from Queue import Empty, Queue
from functools import partial
from threading import Lock, Thread

from sublime import View


class WorkerThread(object):
    def __init__(self, timeout=5):
        self.timeout = timeout
        self.thread = None
        self.queue = Queue()
        self.lock = Lock()

    def main(self):
        while True:
            try:
                f = self.queue.get(True, self.timeout)
            except Empty:
                # Time to clean up this thread, make sure not to miss anything.
                with self.lock:
                    # Something may have arrived just after Empty was raised.
                    # Check again, this time holding a critical section.
                    if self.queue.empty():
                        self.thread = None
                        break
            else:
                f()

    def execute(self, _f, *args, **kwargs):
        with self.lock:
            self.queue.put(partial(_f, *args, **kwargs))

            # If there was no thread to handle the request, create it.
            if self.thread is None:
                self.thread = Thread(target=self.main)
                self.thread.start()


# Default worker thread.
async_worker = WorkerThread()
