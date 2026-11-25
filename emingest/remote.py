# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Christoph T. Koch
"""Files on another computer, reached over SSH (SFTP): the elabFTW ``.eln`` export on the elabFTW server and the
acquisition folders on the lab storage.

Locations are written like for ``scp``::

    user@host:/var/elabftw/exports/2026-11-25-export.eln
    sftp://user@host:2222/data/tem/session_12        (port other than 22)
    /local/path/on/this/computer                     (no host: a local file or folder)

Authentication: the user's SSH agent / keys (``~/.ssh``) where available, a private key given as text, or a
password; credentials are kept in memory only.  Host keys are checked against ``known_hosts``; an unknown host
is refused with its fingerprint, and can be accepted for the session by passing that fingerprint
(``trust_fingerprint``) after comparing it -- never silently.

With SFTP, files are opened for random access: an ``.eln`` archive is read without transferring all of it (only
the index, the metadata and the chosen attachments).  Needs ``paramiko`` (``pip install emingest[remote]``);
without it, whole files are copied with the ``scp`` command (keys / agent only).
"""
from __future__ import annotations

import base64
import hashlib
import io
import os
import posixpath
import re
import shutil
import stat
import subprocess
import tempfile


class RemoteError(RuntimeError):
    pass


class UnknownHostKey(RemoteError):
    def __init__(self, host, fingerprint, key_type):
        self.host, self.fingerprint, self.key_type = host, fingerprint, key_type
        super().__init__(f"the host key of {host} is not known ({key_type} {fingerprint}): compare it with the key "
                         f"of the server and accept it to continue")


SPEC_RE = re.compile(r"^(?:(?P<user>[^@/\s:]+)@)?(?P<host>\[[^\]]+\]|[^:/\s]+):(?P<path>.*)$")


def parse(spec):
    """dict(user, host, port, path) or dict(host=None, path) for a local path."""
    s = str(spec or "").strip()
    if not s:
        raise RemoteError("give a location (path or user@host:/path)")
    if s.startswith(("sftp://", "ssh://", "scp://")):
        from urllib.parse import unquote, urlparse
        u = urlparse(s)
        return {"user": u.username, "host": u.hostname, "port": u.port or 22, "path": unquote(u.path or "/"),
                "password": u.password}
    if re.match(r"^[A-Za-z]:[\\/]", s) or s.startswith(("/", "~", ".")):
        return {"host": None, "path": os.path.expanduser(s)}
    m = SPEC_RE.match(s)
    if m:
        return {"user": m.group("user"), "host": m.group("host").strip("[]"), "port": 22,
                "path": m.group("path") or "."}
    return {"host": None, "path": os.path.expanduser(s)}


def fingerprint(key):
    return "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")


class _Policy:
    def __init__(self, trust):
        self.trust = trust

    def missing_host_key(self, client, hostname, key):
        fp = fingerprint(key)
        if self.trust and fp == self.trust:
            client.get_host_keys().add(hostname, key.get_name(), key)
            return
        raise UnknownHostKey(hostname, fp, key.get_name())


def _load_key(text, passphrase=None):
    import paramiko
    errs = []
    for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            return cls.from_private_key(io.StringIO(text), password=passphrase or None)
        except Exception as e:  # noqa: BLE001
            errs.append(str(e))
    raise RemoteError("the private key could not be read (format or passphrase): " + "; ".join(errs[:1]))


class Location:
    """A file or folder, local or on an SSH server.  Use as context manager (closes the connection)."""

    def __init__(self, spec, password=None, key_text=None, key_passphrase=None, port=None, trust_fingerprint=None,
                 known_hosts=None, timeout=20, allowed_local_roots=None):
        self.p = parse(spec)
        if port:
            self.p["port"] = int(port)
        self.spec = str(spec)
        self.sftp = self.client = None
        self.remote = bool(self.p.get("host"))
        if not self.remote:
            path = os.path.abspath(self.p["path"])
            if allowed_local_roots is not None:
                ok = any(os.path.commonpath([path, os.path.abspath(r)]) == os.path.abspath(r)
                         for r in allowed_local_roots if r)
                if not ok:
                    raise RemoteError("this folder of the server may not be read from the dashboard (allowed: "
                                      + (", ".join(allowed_local_roots) or "none; set EMINGEST_LOCAL_ROOTS") + ")")
            self.p["path"] = path
            return
        try:
            import paramiko
        except ImportError:
            self.paramiko = None
            if password or key_text:
                raise RemoteError("paramiko is needed for passwords or keys given here (pip install paramiko)") \
                    from None
            return
        self.paramiko = paramiko
        c = paramiko.SSHClient()
        try:
            c.load_system_host_keys()
            if known_hosts and os.path.isfile(known_hosts):
                c.load_host_keys(known_hosts)
        except Exception:  # noqa: BLE001
            pass
        c.set_missing_host_key_policy(_Policy(trust_fingerprint))
        kw = dict(hostname=self.p["host"], port=int(self.p.get("port") or 22),
                  username=self.p.get("user") or None, timeout=timeout, banner_timeout=timeout,
                  auth_timeout=timeout)
        pw = password or self.p.get("password")
        if key_text:
            kw["pkey"] = _load_key(key_text, key_passphrase)
            kw.update(allow_agent=False, look_for_keys=False)
        if pw:
            kw["password"] = pw
            if not key_text:
                kw.update(allow_agent=False, look_for_keys=False)
        try:
            c.connect(**kw)
        except UnknownHostKey:
            c.close()
            raise
        except paramiko.AuthenticationException:
            c.close()
            raise RemoteError(f"SSH login to {self.p['host']} refused (user, password or key)") from None
        except Exception as e:  # noqa: BLE001
            c.close()
            raise RemoteError(f"no SSH connection to {self.p['host']}: {e}") from None
        self.client, self.sftp = c, c.open_sftp()

    # ------------------------------------------------------------------ helpers
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def close(self):
        for x in (self.sftp, self.client):
            try:
                if x:
                    x.close()
            except Exception:  # noqa: BLE001
                pass
        self.sftp = self.client = None

    @property
    def path(self):
        return self.p["path"]

    def label(self):
        if not self.remote:
            return self.path
        return f"{self.p.get('user') + '@' if self.p.get('user') else ''}{self.p['host']}:{self.path}"

    def child(self, rel):
        return (posixpath if self.remote else os.path).join(self.path, rel)

    def is_dir(self, path=None):
        path = path or self.path
        if not self.remote:
            return os.path.isdir(path)
        if self.sftp:
            try:
                return stat.S_ISDIR(self.sftp.stat(path).st_mode)
            except OSError:
                return False
        return self._ssh(["test", "-d", path], check=False) == 0

    def exists(self, path=None):
        path = path or self.path
        if not self.remote:
            return os.path.exists(path)
        if self.sftp:
            try:
                self.sftp.stat(path)
                return True
            except OSError:
                return False
        return self._ssh(["test", "-e", path], check=False) == 0

    def walk(self, path=None, recursive=True, max_files=100000):
        """[(relative path, size, mtime)] of the files below the folder (hidden files left out)."""
        root = path or self.path
        out = []
        if not self.remote:
            if not os.path.isdir(root):
                raise RemoteError(f"folder not found: {root}")
            for r, ds, fs in os.walk(root):
                ds[:] = sorted(d for d in ds if not d.startswith("."))
                for f in sorted(fs):
                    if f.startswith("."):
                        continue
                    st = os.stat(os.path.join(r, f))
                    out.append((os.path.relpath(os.path.join(r, f), root).replace(os.sep, "/"), st.st_size,
                                st.st_mtime))
                if not recursive or len(out) > max_files:
                    break
            return out
        if not self.sftp:
            raise RemoteError("listing a remote folder needs paramiko (pip install paramiko)")
        stack = [""]
        while stack:
            rel = stack.pop(0)
            try:
                entries = sorted(self.sftp.listdir_attr(posixpath.join(root, rel)), key=lambda a: a.filename)
            except OSError as e:
                raise RemoteError(f"folder not found on {self.p['host']}: {posixpath.join(root, rel)} ({e})") \
                    from None
            for a in entries:
                if a.filename.startswith("."):
                    continue
                r = posixpath.join(rel, a.filename) if rel else a.filename
                if stat.S_ISDIR(a.st_mode or 0):
                    if recursive:
                        stack.append(r)
                else:
                    out.append((r, int(a.st_size or 0), float(a.st_mtime or 0)))
            if len(out) > max_files:
                break
        return out

    def open(self, path=None):
        """Binary file object for random access (SFTP) or a local file."""
        path = path or self.path
        if not self.remote:
            return open(path, "rb")
        if not self.sftp:
            raise RemoteError("random access needs paramiko; use fetch() instead")
        f = self.sftp.open(path, "rb")
        f.set_pipelined(True)
        return _Buffered(f)

    def fetch(self, path, dest, progress=None, chunk=4 << 20):
        """Copy one file to ``dest`` (local path).  ``progress(done, total)``."""
        os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)
        if not self.remote:
            shutil.copyfile(path, dest)
            return dest
        if self.sftp:
            total = self.sftp.stat(path).st_size or 0
            done = 0
            with self.sftp.open(path, "rb") as src, open(dest, "wb") as fo:
                src.prefetch(total)
                while True:
                    b = src.read(chunk)
                    if not b:
                        break
                    fo.write(b)
                    done += len(b)
                    if progress:
                        progress(done, total)
            return dest
        target = f"{self.p.get('user') + '@' if self.p.get('user') else ''}{self.p['host']}:{path}"
        r = subprocess.run(["scp", "-q", "-B", "-P", str(self.p.get("port") or 22), target, dest],
                           capture_output=True, text=True)
        if r.returncode:
            raise RemoteError(f"scp failed: {r.stderr.strip()[:300]}")
        return dest

    def _ssh(self, cmd, check=True):
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-p", str(self.p.get("port") or 22),
                            f"{self.p.get('user') + '@' if self.p.get('user') else ''}{self.p['host']}"]
                           + [subprocess.list2cmdline([c]) for c in cmd], capture_output=True, text=True)
        if check and r.returncode:
            raise RemoteError(r.stderr.strip()[:300])
        return r.returncode


class _Buffered(io.RawIOBase):
    """Read-ahead around an SFTP file (zipfile reads in small pieces)."""

    def __init__(self, f, block=1 << 20):
        self.f, self.block = f, block
        self.pos, self.buf_start, self.buf = 0, 0, b""
        self.size = f.stat().st_size

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, off, whence=0):
        self.pos = off if whence == 0 else self.pos + off if whence == 1 else self.size + off
        return self.pos

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        if n <= 0 or self.pos >= self.size:
            return b""
        end = self.pos + n
        if not (self.buf_start <= self.pos and end <= self.buf_start + len(self.buf)):
            if n >= self.block:
                self.f.seek(self.pos)
                data = self.f.read(n)
                self.pos += len(data)
                return data
            self.f.seek(self.pos)
            self.buf_start, self.buf = self.pos, self.f.read(self.block)
        i = self.pos - self.buf_start
        data = self.buf[i:i + n]
        self.pos += len(data)
        return data

    def readinto(self, b):
        d = self.read(len(b))
        b[:len(d)] = d
        return len(d)

    def close(self):
        try:
            self.f.close()
        finally:
            super().close()


def open_eln(spec, dest_dir=None, **kw):
    """(ElnArchive, Location) for an .eln file given as path or user@host:/path.  Remote archives are read in place
    via SFTP (paramiko) or copied into ``dest_dir`` (scp).  The Location must stay open while the archive is used."""
    from .eln_archive import ElnArchive
    loc = Location(spec, **kw)
    if loc.is_dir():
        files = sorted((f for f in loc.walk(recursive=False) if f[0].lower().endswith(".eln")),
                       key=lambda f: f[2])
        if not files:
            loc.close()
            raise RemoteError(f"no .eln file in {loc.label()}")
        loc.p["path"] = loc.child(files[-1][0])         # the newest export of the folder
    name = os.path.basename(loc.path)
    if loc.remote and not loc.sftp:
        dest_dir = dest_dir or tempfile.mkdtemp(prefix="eln_")
        local = loc.fetch(loc.path, os.path.join(dest_dir, name))
        return ElnArchive(local, name=name), loc
    return ElnArchive(loc.open(), name=name), loc
