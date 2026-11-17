"""Offline: the errno and path answers of the file calls, on both packagings. The expected values are what
the real Pyodide 315.0.0-alpha.2 on Node printed for the same scripts; where a comment says Linux, Pyodide's answer is a
defect of its own (it strips a slash that Linux refuses, takes "" for the descriptor, crashes on an access mode of 3)
and Linux's is kept. The refusals of the mutating calls on /dev and /proc are the adapter's own design (a read-only
virtual tree), not Pyodide's."""

from __future__ import annotations

import sys

from conftest import Guest

WIN = sys.platform == "win32"

HEAD = """
import os, errno, shutil, ctypes, tempfile

def err(f, *a, **k):
    try:
        f(*a, **k)
    except OSError as e:
        return errno.errorcode[e.errno]
    return None

libc = ctypes.CDLL(None, use_errno=True)

def raw(fn, *a):  # a libc call: 0 / the value, or the errno name
    ctypes.set_errno(0)
    r = fn(*a)
    return r if r >= 0 else errno.errorcode[ctypes.get_errno()]

W = tempfile.mkdtemp()
os.chdir(W)
open('f', 'w').write('x')
os.mkdir('d')
"""


def run(guest: Guest, body: str) -> None:
    # the script runs through exec with its source registered, so a failure shows the line, not only its number
    script = f"WIN = {WIN}\nLINUX = {sys.platform.startswith('linux')}\n" + HEAD + body
    code = (
        f"import linecache\nS = {script!r}\nlinecache.cache['<t>'] = (len(S), None, S.splitlines(True), '<t>')\n"
        "exec(compile(S, '<t>', 'exec'))\nprint('ok')"
    )
    r = guest.run(code, None)
    err_lines = [ln for ln in r.stderr.splitlines() if not ln.startswith("[host]")]  # the traceback, not the host's log
    assert r.returncode == 0 and r.stdout.rstrip().endswith("ok"), (r.stdout[-1500:], "\n".join(err_lines)[-2500:])


def test_the_empty_path_is_enoent_in_every_call(guest: Guest):
    run(
        guest,
        """
dfd = os.open('.', os.O_RDONLY)
for dir_fd in (None, dfd, 99):  # (a bad descriptor does not matter: the path is looked at first)
    kw = {} if dir_fd is None else {'dir_fd': dir_fd}
    assert err(os.open, '', os.O_RDONLY, **kw) == 'ENOENT', dir_fd
    assert err(os.stat, '', **kw) == 'ENOENT', dir_fd
    assert err(os.unlink, '', **kw) == 'ENOENT', dir_fd
    assert err(os.mkdir, '', **kw) == 'ENOENT', dir_fd
    assert err(os.readlink, '', **kw) == 'ENOENT', dir_fd
    assert err(os.rmdir, '', **kw) == 'ENOENT', dir_fd
    rkw = {} if dir_fd is None else {'src_dir_fd': dir_fd, 'dst_dir_fd': dir_fd}
    assert err(os.rename, '', 'x', **rkw) == 'ENOENT', dir_fd
assert err(os.open, '', os.O_RDWR | os.O_CREAT) == 'ENOENT'
assert err(os.rename, 'f', '') == 'ENOENT' and err(os.symlink, 'f', '') == 'ENOENT'
assert err(os.truncate, '', 0) == 'ENOENT'
assert err(os.listdir, '') == 'ENOENT' and err(lambda: list(os.scandir(''))) == 'ENOENT'
assert err(os.lstat, '') == 'ENOENT' and err(os.chdir, '') == 'ENOENT' and err(os.statvfs, '') == 'ENOENT'
assert err(os.chmod, '', 0o755) == 'ENOENT' and err(os.chown, '', 0, 0) == 'ENOENT'
assert not os.access('', os.F_OK)
# linkat with an empty path (os.link is not in Pyodide's os)
linkat = libc.linkat
assert raw(linkat, -100, b'', -100, b'z', 0) == 'ENOENT' and raw(linkat, -100, b'f', -100, b'', 0) == 'ENOENT'
# utimensat: "" is ENOENT (Linux; Pyodide takes it as the descriptor), os.utime(fd) is futimens (a null path) and works
assert err(os.utime, '', (1, 1)) == 'ENOENT' and err(os.utime, '', (1, 1), dir_fd=dfd) == 'ENOENT'
if not WIN:  # a Windows host's directory handle lacks write-attributes access (EACCES)
    _u = err(os.utime, dfd, (1, 1))
    assert _u is None, _u
    assert os.stat('d').st_mtime > 1e9 and os.stat(dfd).st_mtime == 1
# AT_EMPTY_PATH of fstatat is the descriptor (or the cwd); without it "" is ENOENT even for a bad descriptor (Linux)
buf = ctypes.create_string_buffer(256)
assert raw(libc.fstatat, dfd, b'', buf, 0x1000) == 0 and raw(libc.fstatat, -100, b'', buf, 0x1000) == 0
assert raw(libc.fstatat, 99, b'', buf, 0x1000) == 'EBADF'
assert raw(libc.fstatat, 99, b'', buf, 0) == 'ENOENT' and raw(libc.fstatat, dfd, b'', buf, 0) == 'ENOENT'
""",
    )


def test_rmdir_with_trailing_slashes(guest: Guest):
    run(
        guest,
        """
os.mkdir('e')
assert err(os.rmdir, 'e/') is None and not os.path.exists('e')
os.mkdir('e')
assert err(os.rmdir, 'e//') is None and not os.path.exists('e')
os.makedirs('e/sub')
open('e/sub/h', 'w').write('x')
assert err(os.rmdir, 'e/') == 'ENOTEMPTY'
shutil.rmtree('e/')
assert not os.path.exists('e')
_r = (err(os.rmdir, 'f/'), err(os.rmdir, 'nothere/'), err(os.rmdir, '.'))
assert _r == ('ENOTDIR', 'ENOENT', 'EINVAL'), _r
# the last component decides: "." is EINVAL, ".." is ENOTEMPTY (a Windows host says EACCES for both)
os.mkdir('e')
_r = (err(os.rmdir, 'd/.'), err(os.rmdir, 'd/..'), err(os.rmdir, 'e/./'), err(os.rmdir, '..'), err(os.rmdir, './.'))
assert _r == ('EINVAL', 'ENOTEMPTY', 'EINVAL', 'ENOTEMPTY', 'EINVAL'), _r
assert os.path.isdir('d') and os.path.isdir('e')
# the prefix is looked at first, as on Linux
_r = [err(os.rmdir, x) for x in ('missing/.', 'missing/..', 'f/.', 'f/..', 'd/./.', 'missing/./')]
assert _r == ['ENOENT', 'ENOENT', 'ENOTDIR', 'ENOTDIR', 'EINVAL', 'ENOENT'], _r
os.rmdir('e')
os.rmdir(os.path.join(W, 'd') + '/')  # absolute, too
assert not os.path.exists('d')
os.mkdir('d')
""",
    )


def test_read_of_a_directory_descriptor_is_eisdir(guest: Guest):
    run(
        guest,
        """
for p in ('d', '/dev', '/proc/self/fd', '/dev/shm'):
    fd = os.open(p, os.O_RDONLY)
    assert err(os.read, fd, 10) == 'EISDIR', p
    assert err(os.pread, fd, 10, 0) == 'EISDIR', p
    assert err(os.readv, fd, [bytearray(10)]) == 'EISDIR', p
    assert err(os.write, fd, b'x') in ('EBADF', 'EISDIR'), p
    os.close(fd)
try:
    open('d').read()
except IsADirectoryError:
    pass
else:
    raise AssertionError
# a write-only file is still EBADF
fd = os.open('f', os.O_WRONLY)
_w = (err(os.read, fd, 1), err(os.pread, fd, 1, 0))
assert _w == ('EBADF', 'EBADF'), _w
""",
    )


def test_open_of_a_directory_for_writing_is_eisdir(guest: Guest):
    run(
        guest,
        """
for fl in (os.O_WRONLY, os.O_RDWR, os.O_WRONLY | os.O_CREAT, os.O_RDWR | os.O_TRUNC, os.O_WRONLY | os.O_APPEND):
    _o = err(os.open, 'd', fl)
    assert _o == 'EISDIR', (fl, _o)
    assert err(os.open, '.', fl) == 'EISDIR', fl
assert err(os.open, 'd', os.O_WRONLY | os.O_DIRECTORY) == 'EISDIR'
assert err(os.open, 'f', os.O_DIRECTORY) == 'ENOTDIR'
for m in ('w', 'a', 'r+'):
    assert err(open, 'd', m) == 'EISDIR', m
""",
    )


def test_a_directory_opened_for_writing_is_eisdir_whatever_its_mode(guest: Guest):
    # (before: the mode bits were looked at first, EACCES); O_CREAT|O_EXCL on "d/" is EEXIST as Pyodide (Linux: EISDIR)
    run(
        guest,
        """
os.mkdir('dro'); os.chmod('dro', 0o555)
os.mkdir('dnone'); os.chmod('dnone', 0)
for n in ('dro', 'dnone'):
    for fl in (os.O_WRONLY, os.O_RDWR, os.O_RDONLY | os.O_CREAT, os.O_RDONLY | os.O_TRUNC, os.O_WRONLY | os.O_APPEND):
        if WIN and fl & os.O_CREAT:  # (a Windows host answers what it answers for a create on a directory: unchecked)
            continue
        assert err(os.open, n, fl) == 'EISDIR', (n, fl)
for fl in (os.O_WRONLY, os.O_RDWR):
    if LINUX:  # (the host's errno for "d/" with O_CREAT|O_EXCL differs on macOS and Windows: unchecked there)
        assert err(os.open, 'd/', fl | os.O_CREAT | os.O_EXCL) == 'EEXIST', fl
    assert err(os.open, 'd', fl | os.O_CREAT | os.O_EXCL) == 'EEXIST', fl
""",
    )


def test_getcwd_of_a_removed_directory_is_enoent(guest: Guest):
    # Linux; Pyodide refuses to remove the cwd (EBUSY), which this does not copy
    run(
        guest,
        """
os.mkdir('gone'); os.mkdir('gone/in')
os.chdir('gone/in')
assert os.getcwd() == W + '/gone/in'
os.rmdir(W + '/gone/in')
assert err(os.getcwd) == 'ENOENT'
os.chdir(W)
assert os.getcwd() == W
""",
    )


def test_f_getfl_of_an_opened_file_has_o_largefile_the_standard_streams_do_not(guest: Guest):
    run(
        guest,
        """
import fcntl
LF = 0o100000
for target in ('f', 'd', '/dev/null'):
    fd = os.open(target, os.O_RDONLY)
    assert fcntl.fcntl(fd, fcntl.F_GETFL) == LF, (target, oct(fcntl.fcntl(fd, fcntl.F_GETFL)))
fd = os.open('f', os.O_RDWR | os.O_APPEND)
assert fcntl.fcntl(fd, fcntl.F_GETFL) == LF | 0o2002
assert [fcntl.fcntl(i, fcntl.F_GETFL) for i in (0, 1, 2)] == [0, 1, 1]
""",
    )


def test_writev_on_a_standard_stream_writes_every_buffer(guest: Guest):
    p = guest.run("import os\nn = os.writev(1, [b'abc', b'def', b'\\n'])\nos.writev(2, [b'x', b'y'])\nprint(n)")
    assert p.returncode == 0, p.stderr[-2000:]
    assert p.stdout.splitlines() == ["abcdef", "7"], p.stdout
    assert "xy" in p.stderr


def test_the_root_lists_dev_and_proc_once(guest: Guest):
    run(
        guest,
        """
for p in ('/', '/.', '//', '/tmp/..'):
    names = os.listdir(p)
    assert names.count('dev') == 1 and names.count('proc') == 1, (p, names)
    assert sorted(e.name for e in os.scandir(p)).count('dev') == 1, p
os.chdir('/')
assert 'dev' in os.listdir('.') and 'proc' in os.listdir('.')
assert 'dev' not in os.listdir('/tmp') and 'dev' not in os.listdir(W)
""",
    )


def test_standard_streams_stat_and_times(guest: Guest):
    run(
        guest,
        """
for i in (0, 1, 2):
    assert os.fstat(i).st_nlink == 1, i
    os.utime(i, (1, 1))
    os.utime(i)
    os.utime(i, ns=(1, 1))
""",
    )


def test_dup3_fadvise_access_validation(guest: Guest):
    run(
        guest,
        """
dup3 = libc.dup3
assert raw(dup3, 1, 40, os.O_CLOEXEC) == 40 and raw(dup3, 1, 41, 0) == 41
for fl in (os.O_NONBLOCK, os.O_APPEND, 1, os.O_CLOEXEC | 4):
    assert raw(dup3, 1, 42, fl) == 'EINVAL', fl
assert raw(dup3, 1, 1, os.O_CLOEXEC) == 'EINVAL' and raw(dup3, 77, 45, 0) == 'EBADF' and raw(dup3, 1, -1, 0) == 'EBADF'
fa = libc.posix_fadvise
fa.argtypes = [ctypes.c_int, ctypes.c_int64, ctypes.c_int64, ctypes.c_int]
fd = os.open('f', os.O_RDONLY)
assert [fa(fd, 0, 0, a) for a in range(6)] == [0] * 6
for a in (6, 7, 100, -1):  # Linux: EINVAL (Pyodide takes any number)
    assert fa(fd, 0, 0, a) == errno.EINVAL, a
assert fa(99, 0, 0, 0) == errno.EBADF and fa(99, 0, 0, 99) == errno.EBADF
assert fa(os.open('d', os.O_RDONLY), 0, 0, 9) == errno.EINVAL
assert fa(1, 0, 0, 0) == errno.ESPIPE  # (a standard stream is not seekable here, as in Pyodide)
for m in (8, 16, 0x100, -1):
    assert raw(libc.access, b'f', m) == 'EINVAL', m
assert raw(libc.access, b'nothere', 8) == 'EINVAL'
assert raw(libc.access, b'f', 4) == 0
""",
    )


def test_truncate_by_path_needs_a_writable_file(guest: Guest):
    """Pyodide's truncate(path) checks the mode like open() for writing (EACCES for a 0444 file); Linux lets root."""
    run(
        guest,
        """
open('ro', 'w').write('abc')
os.chmod('ro', 0o444)
assert err(os.truncate, 'ro', 0) == 'EACCES' and err(os.open, 'ro', os.O_WRONLY) == 'EACCES'
assert open('ro').read() == 'abc'
os.chmod('ro', 0o644)
os.truncate('ro', 1)
assert open('ro').read() == 'a'
assert err(os.truncate, 'nothere', 0) == 'ENOENT' and err(os.truncate, 'd', 0) == 'EISDIR'
""",
    )


def test_open_with_access_mode_3_is_open(guest: Guest):
    """Linux opens it (neither read nor write checked, for ioctl); Pyodide dies on it with a JavaScript TypeError."""
    run(
        guest,
        """
fd = os.open('f', 3)
assert fd >= 3
os.close(fd)
""",
    )


def test_chdir_into_the_virtual_directories(guest: Guest):
    run(
        guest,
        """
for p in ('/dev', '/proc', '/proc/self', '/proc/self/fd', '/dev/shm'):
    os.chdir(W)
    os.chdir(p)
    assert os.getcwd() == p, p
    os.chdir(W)
    fd = os.open(p, os.O_RDONLY)
    os.fchdir(fd)
    assert os.getcwd() == p, p
    os.close(fd)
os.chdir('/')
os.chdir('dev')
assert os.getcwd() == '/dev'
os.chdir('/')
os.chdir('./proc/self')
assert os.getcwd() == '/proc/self'
os.chdir(W)
assert err(os.chdir, '/dev/null') == 'ENOTDIR' and err(os.chdir, '/dev/nothere') == 'ENOENT'
fd = os.open('/dev/null', os.O_RDONLY)
assert err(os.fchdir, fd) == 'ENOTDIR'
assert os.getcwd() == W
# in /dev, relative names are the virtual ones, and ".." leaves the virtual tree
os.chdir('/dev')
assert sorted(os.listdir('.')) == sorted(os.listdir('/dev')) and os.stat('null').st_rdev == 259
assert os.path.exists('../tmp') == os.path.exists('/tmp') and os.path.samefile('..', '/')
assert os.path.exists('shm/../null') and not os.path.exists('nothere')
os.chdir('..')
assert os.getcwd() == '/'
os.chdir('/proc/self/fd')
assert err(os.chdir, '..') is None and os.getcwd() == '/proc/self'
os.chdir('/dev/shm/../../proc')
assert os.getcwd() == '/proc'
os.chdir(W)
""",
    )


def test_virtual_nodes_refuse_the_mutating_calls_alike(guest: Guest):
    # EACCES is the W2 design choice (nothing is made, removed or replaced under /dev or /proc): Pyodide's MEMFS lets
    # rename('f', '/dev/null') and rename('/dev/null', 'g') succeed and says ENOENT/ENOTEMPTY for other names
    run(
        guest,
        """
assert err(os.rename, 'f', '/dev/null') == 'EACCES' and err(os.rename, 'f', '/dev/x') == 'EACCES'
assert err(os.rename, '/dev/null', 'g') == 'EACCES' and err(os.mkdir, '/dev/null') == 'EEXIST'
assert err(os.symlink, 'f', '/dev/null') == 'EEXIST' and err(os.unlink, '/dev/null') == 'EACCES'
assert os.path.exists('f') and os.path.exists('/dev/null')
""",
    )


def test_mknod_under_dev_is_refused_like_every_create_and_fchdir_wants_a_directory(guest: Guest):
    run(
        guest,
        """
assert libc.mknod(b'/dev/zz', 0o100600, 0) == 2 and err(os.open, '/dev/zz', os.O_CREAT | os.O_WRONLY) == 'EACCES'
f = os.open('f', os.O_RDONLY)  # (mknod: libc returns the errno itself here, 2 is EACCES; 44, ENOENT, before)
assert err(os.fchdir, f) == 'ENOTDIR'
if not WIN:  # (a Windows host cannot rename an open file)
    os.rename('f', 'g'); os.mkdir('f')  # the file's old path is a directory now (Pyodide goes there, Linux: ENOTDIR)
    assert err(os.fchdir, f) == 'ENOTDIR'
os.chdir(W)
""",
    )
