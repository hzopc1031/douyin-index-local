"""Windows ACL groundwork. Not yet used by the comment entrypoint.

A new empty directory is protected using its open handle before any data is
written. Full file-opening and reparse-point protections are separate gates.
"""
from pathlib import Path
import stat
import sys

from .errors import Stop


def _modules():
    if sys.platform != 'win32':
        raise Stop(5, 'windows_only')
    try:
        import win32api
        import win32con
        import win32file
        import win32security
        import ntsecuritycon
    except ImportError:
        raise Stop(5, 'windows_acl_unavailable') from None
    return win32api, win32con, win32file, win32security, ntsecuritycon


def create_private_empty_directory(path):
    """Create and protect a new empty Windows directory; refuse existing paths."""
    api, con, file, security, nt = _modules()
    target = Path(path).expanduser().absolute()
    for part in target.parents:
        try:
            info = part.lstat()
        except OSError:
            raise Stop(5, 'directory_unavailable') from None
        if getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise Stop(5, 'directory_symlink')
    made = False
    handle = None
    try:
        target.mkdir()
        made = True
        handle = file.CreateFile(str(target), con.READ_CONTROL | con.WRITE_DAC | nt.FILE_READ_ATTRIBUTES,
                                 con.FILE_SHARE_READ | con.FILE_SHARE_WRITE | con.FILE_SHARE_DELETE,
                                 None, con.OPEN_EXISTING,
                                 con.FILE_FLAG_BACKUP_SEMANTICS | con.FILE_FLAG_OPEN_REPARSE_POINT, None)
        attributes = file.GetFileInformationByHandle(handle)[0]
        if attributes & con.FILE_ATTRIBUTE_REPARSE_POINT:
            raise Stop(5, 'directory_symlink')
        token = security.OpenProcessToken(api.GetCurrentProcess(), con.TOKEN_QUERY)
        try:
            user = security.GetTokenInformation(token, security.TokenUser)[0]
        finally:
            token.Close()
        system = security.ConvertStringSidToSid('S-1-5-18')
        acl = security.ACL()
        inheritance = security.OBJECT_INHERIT_ACE | security.CONTAINER_INHERIT_ACE
        for sid in (user, system):
            acl.AddAccessAllowedAceEx(security.ACL_REVISION, inheritance, nt.FILE_ALL_ACCESS, sid)
        security.SetSecurityInfo(handle, security.SE_FILE_OBJECT,
            security.DACL_SECURITY_INFORMATION | security.PROTECTED_DACL_SECURITY_INFORMATION,
            None, None, acl, None)
        descriptor = security.GetSecurityInfo(handle, security.SE_FILE_OBJECT,
            security.OWNER_SECURITY_INFORMATION | security.DACL_SECURITY_INFORMATION)
        if not security.EqualSid(descriptor.GetSecurityDescriptorOwner(), user):
            raise Stop(5, 'directory_permissions')
        if not (descriptor.GetSecurityDescriptorControl()[0] & security.SE_DACL_PROTECTED):
            raise Stop(5, 'directory_permissions')
        actual = descriptor.GetSecurityDescriptorDacl()
        if actual is None or actual.GetAceCount() != 2:
            raise Stop(5, 'directory_permissions')
        entries = [actual.GetAce(i) for i in range(actual.GetAceCount())]
        allowed = {security.ConvertSidToStringSid(user), security.ConvertSidToStringSid(system)}
        if {security.ConvertSidToStringSid(entry[2]) for entry in entries} != allowed:
            raise Stop(5, 'directory_permissions')
        if any(entry[0][0] != security.ACCESS_ALLOWED_ACE_TYPE or entry[1] & nt.FILE_ALL_ACCESS != nt.FILE_ALL_ACCESS
               for entry in entries):
            raise Stop(5, 'directory_permissions')
        return target
    except Stop:
        raise
    except Exception:
        raise Stop(5, 'directory_permissions') from None
    finally:
        if handle is not None:
            handle.Close()
        # On failure remove only the empty directory this call created.
        if made and sys.exc_info()[0] is not None:
            try:
                target.rmdir()
            except OSError:
                pass
