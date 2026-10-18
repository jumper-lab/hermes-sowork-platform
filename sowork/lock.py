"""OS-held subscription lease, also valid for embedded GatewayRunner processes."""
import hashlib
import os


class SubscriptionLock:
    def __init__(self, scope):
        from gateway.status import _get_lock_dir
        self.path = _get_lock_dir() / ("sowork-" + hashlib.sha256(scope.encode()).hexdigest() + ".flock")
        self.handle = None

    def acquire(self):
        import fcntl
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        self.handle = fd
        return True

    def release(self):
        if self.handle is not None:
            os.close(self.handle)
            self.handle = None
        # Never unlink: replacing the inode while peers wait would split locks.
