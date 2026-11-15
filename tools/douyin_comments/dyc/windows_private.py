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


def _user_sid(api, con, security):
    token = security.OpenProcessToken(api.GetCurrentProcess(), con.TOKEN_QUERY)
    try:
        return security.GetTokenInformation(token, security.TokenUser)[0]
    finally:
        token.Close()


def _validate_descriptor(descriptor, user, security, nt):
    system = security.ConvertStringSidToSid('S-1-5-18')
    if security.ConvertSidToStringSid(descriptor.GetSecurityDescriptorOwner()) != security.ConvertSidToStringSid(user):
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
    inheritance = security.OBJECT_INHERIT_ACE | security.CONTAINER_INHERIT_ACE
    if any(entry[0][0] != security.ACCESS_ALLOWED_ACE_TYPE or entry[0][1] != inheritance
           or entry[1] & nt.FILE_ALL_ACCESS != nt.FILE_ALL_ACCESS
           for entry in entries):
        raise Stop(5, 'directory_permissions')


def verify_private_directory(path):
    """Check an existing directory; not a substitute for handle-relative file opens."""
    api, con, file, security, nt = _modules()
    target = Path(path).expanduser().absolute()
    for part in (target, *target.parents):
        try:
            info = part.lstat()
        except OSError:
            raise Stop(5, 'directory_unavailable') from None
        if getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise Stop(5, 'directory_symlink')
    handle = None
    try:
        handle = file.CreateFile(str(target), con.READ_CONTROL | nt.FILE_READ_ATTRIBUTES,
                                 con.FILE_SHARE_READ | con.FILE_SHARE_WRITE | con.FILE_SHARE_DELETE,
                                 None, con.OPEN_EXISTING, con.FILE_FLAG_BACKUP_SEMANTICS | 0x00200000, None)
        attributes = file.GetFileInformationByHandle(handle)[0]
        if attributes & con.FILE_ATTRIBUTE_REPARSE_POINT:
            raise Stop(5, 'directory_symlink')
        if not (attributes & con.FILE_ATTRIBUTE_DIRECTORY):
            raise Stop(5, 'directory_permissions')
        descriptor = security.GetSecurityInfo(handle, security.SE_FILE_OBJECT,
            security.OWNER_SECURITY_INFORMATION | security.DACL_SECURITY_INFORMATION)
        _validate_descriptor(descriptor, _user_sid(api, con, security), security, nt)
    except Stop:
        raise
    except Exception:
        raise Stop(5, 'directory_permissions') from None
    finally:
        if handle is not None:
            handle.Close()


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
    stage = 'create'
    try:
        target.mkdir()
        made = True
        stage = 'open'
        handle = file.CreateFile(str(target), con.READ_CONTROL | con.WRITE_DAC | con.WRITE_OWNER | nt.FILE_READ_ATTRIBUTES,
                                 con.FILE_SHARE_READ | con.FILE_SHARE_WRITE | con.FILE_SHARE_DELETE,
                                 None, con.OPEN_EXISTING,
                                 con.FILE_FLAG_BACKUP_SEMANTICS | 0x00200000, None)  # FILE_FLAG_OPEN_REPARSE_POINT
        stage = 'attributes'
        attributes = file.GetFileInformationByHandle(handle)[0]
        if attributes & con.FILE_ATTRIBUTE_REPARSE_POINT:
            raise Stop(5, 'directory_symlink')
        stage = 'identity'
        user = _user_sid(api, con, security)
        stage = 'build_acl'
        system = security.ConvertStringSidToSid('S-1-5-18')
        acl = security.ACL()
        inheritance = security.OBJECT_INHERIT_ACE | security.CONTAINER_INHERIT_ACE
        for sid in (user, system):
            acl.AddAccessAllowedAceEx(security.ACL_REVISION, inheritance, nt.FILE_ALL_ACCESS, sid)
        stage = 'set_acl'
        security.SetSecurityInfo(handle, security.SE_FILE_OBJECT,
            security.OWNER_SECURITY_INFORMATION | security.DACL_SECURITY_INFORMATION |
            security.PROTECTED_DACL_SECURITY_INFORMATION,
            user, None, acl, None)
        stage = 'verify_acl'
        descriptor = security.GetSecurityInfo(handle, security.SE_FILE_OBJECT,
            security.OWNER_SECURITY_INFORMATION | security.DACL_SECURITY_INFORMATION)
        _validate_descriptor(descriptor, user, security, nt)
        return target
    except Stop:
        raise
    except Exception:
        raise Stop(5, 'windows_acl_' + stage) from None
    finally:
        if handle is not None:
            handle.Close()
        # On failure remove only the empty directory this call created.
        if made and sys.exc_info()[0] is not None:
            try:
                target.rmdir()
            except OSError:
                pass
