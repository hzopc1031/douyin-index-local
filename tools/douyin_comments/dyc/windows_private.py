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


def _validate_file_handle(handle, user, con, file, security, nt):
    info = file.GetFileInformationByHandle(handle)
    if info[0] & (con.FILE_ATTRIBUTE_REPARSE_POINT | con.FILE_ATTRIBUTE_DIRECTORY) or info[7] != 1:
        raise Stop(5, 'file_permissions')
    descriptor = security.GetSecurityInfo(handle, security.SE_FILE_OBJECT,
        security.OWNER_SECURITY_INFORMATION | security.DACL_SECURITY_INFORMATION)
    if security.ConvertSidToStringSid(descriptor.GetSecurityDescriptorOwner()) != security.ConvertSidToStringSid(user):
        raise Stop(5, 'file_permissions')
    acl = descriptor.GetSecurityDescriptorDacl()
    if acl is None or acl.GetAceCount() != 2:
        raise Stop(5, 'file_permissions')
    entries = [acl.GetAce(i) for i in range(acl.GetAceCount())]
    allowed = {security.ConvertSidToStringSid(user), 'S-1-5-18'}
    if {security.ConvertSidToStringSid(entry[2]) for entry in entries} != allowed:
        raise Stop(5, 'file_permissions')
    if any(entry[0][0] != security.ACCESS_ALLOWED_ACE_TYPE
           or entry[0][1] & security.INHERIT_ONLY_ACE
           or entry[1] & nt.FILE_ALL_ACCESS != nt.FILE_ALL_ACCESS for entry in entries):
        raise Stop(5, 'file_permissions')


def read_private_file(path, *, max_bytes=256 * 1024):
    """Read from the validated Windows handle; never follow a final reparse point."""
    api, con, file, security, nt = _modules()
    if type(max_bytes) is not int or not 0 <= max_bytes <= 1024 * 1024:
        raise ValueError('invalid size limit')
    target = Path(path).expanduser().absolute()
    verify_private_directory(target.parent)
    parent_handle = handle = None
    try:
        # Keep the verified parent open without delete sharing through the read.
        parent_handle = file.CreateFile(str(target.parent), con.READ_CONTROL | nt.FILE_READ_ATTRIBUTES,
                                        con.FILE_SHARE_READ | con.FILE_SHARE_WRITE, None, con.OPEN_EXISTING,
                                        con.FILE_FLAG_BACKUP_SEMANTICS | 0x00200000, None)
        attrs = file.GetFileInformationByHandle(parent_handle)[0]
        if attrs & con.FILE_ATTRIBUTE_REPARSE_POINT or not attrs & con.FILE_ATTRIBUTE_DIRECTORY:
            raise Stop(5, 'directory_permissions')
        user = _user_sid(api, con, security)
        parent_sd = security.GetSecurityInfo(parent_handle, security.SE_FILE_OBJECT,
            security.OWNER_SECURITY_INFORMATION | security.DACL_SECURITY_INFORMATION)
        _validate_descriptor(parent_sd, user, security, nt)
        handle = file.CreateFile(str(target), con.GENERIC_READ | con.READ_CONTROL | nt.FILE_READ_ATTRIBUTES,
                                 con.FILE_SHARE_READ, None, con.OPEN_EXISTING, 0x00200000, None)
        _validate_file_handle(handle, user, con, file, security, nt)
        _, content = file.ReadFile(handle, max_bytes + 1)
        if len(content) > max_bytes:
            raise Stop(5, 'file_too_large')
        return bytes(content)
    except Stop:
        raise
    except Exception:
        raise Stop(5, 'file_permissions') from None
    finally:
        if handle is not None:
            handle.Close()
        if parent_handle is not None:
            parent_handle.Close()


def verify_private_file(path):
    """Check one existing file, without exposing its content."""
    read_private_file(path)


def write_new_private_file(path, content):
    """Write a new private file. Existing files are never opened for writing."""
    api, con, file, security, nt = _modules()
    if not isinstance(content, bytes) or len(content) > 10 * 1024 * 1024:
        raise ValueError('invalid content')
    target = Path(path).expanduser().absolute()
    if not target.name or ':' in target.name:
        raise Stop(5, 'file_unavailable')
    verify_private_directory(target.parent)
    parent_handle = handle = None
    try:
        parent_handle = file.CreateFile(str(target.parent), con.READ_CONTROL | nt.FILE_READ_ATTRIBUTES,
                                        con.FILE_SHARE_READ | con.FILE_SHARE_WRITE, None, con.OPEN_EXISTING,
                                        con.FILE_FLAG_BACKUP_SEMANTICS | 0x00200000, None)
        attrs = file.GetFileInformationByHandle(parent_handle)[0]
        if attrs & con.FILE_ATTRIBUTE_REPARSE_POINT or not attrs & con.FILE_ATTRIBUTE_DIRECTORY:
            raise Stop(5, 'directory_permissions')
        user = _user_sid(api, con, security)
        parent_sd = security.GetSecurityInfo(parent_handle, security.SE_FILE_OBJECT,
            security.OWNER_SECURITY_INFORMATION | security.DACL_SECURITY_INFORMATION)
        _validate_descriptor(parent_sd, user, security, nt)
        # CREATE_NEW refuses existing files, hard links and reparse points.
        handle = file.CreateFile(str(target), con.GENERIC_WRITE | con.READ_CONTROL | con.WRITE_DAC |
                                 con.WRITE_OWNER | nt.FILE_READ_ATTRIBUTES, con.FILE_SHARE_READ,
                                 None, con.CREATE_NEW, 0x00200000, None)
        system = security.ConvertStringSidToSid('S-1-5-18')
        acl = security.ACL()
        for sid in (user, system):
            acl.AddAccessAllowedAce(security.ACL_REVISION, nt.FILE_ALL_ACCESS, sid)
        security.SetSecurityInfo(handle, security.SE_FILE_OBJECT,
            security.OWNER_SECURITY_INFORMATION | security.DACL_SECURITY_INFORMATION |
            security.PROTECTED_DACL_SECURITY_INFORMATION, user, None, acl, None)
        _validate_file_handle(handle, user, con, file, security, nt)
        if content:
            result, written = file.WriteFile(handle, content)
            if result != 0 or written != len(content):
                raise Stop(5, 'file_unavailable')
        file.FlushFileBuffers(handle)
    except Stop:
        raise
    except Exception:
        raise Stop(5, 'file_unavailable') from None
    finally:
        if handle is not None:
            handle.Close()
        if parent_handle is not None:
            parent_handle.Close()


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
