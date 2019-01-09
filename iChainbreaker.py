import os
import uuid
import argparse

from binascii import hexlify
import sys
from keybag import Keybag
from blobparser import BlobParser
import sqlite3 as lite
from hexdump import hexdump

from exportDB import ExporySQLiteDB

from crypto.aeswrap import AESUnwrap
from crypto.gcm import gcm_decrypt
from crypto.aes import AESdecryptCBC
from ctypes import *

class _EncryptedBlobHeader(LittleEndianStructure):
    _fields_ = [
        ('version', c_uint32),
        ('clas', c_uint32),
        ('length', c_uint32)
    ]
def _memcpy(buf, fmt):
    return cast(c_char_p(buf), POINTER(fmt)).contents


def GetTableFullName(table):
    if table == 'genp':
        return 'Generic Password'
    elif table == 'inet':
        return 'Internet Password'
    elif table == 'cert':
        return 'Certification'
    elif table == 'keys':
        return 'Keys'
    else:
        return 'Unknown'

def main():


    parser = argparse.ArgumentParser(description='Tool for iCloud Keychain Analysis by @n0fate')
    parser.add_argument('-p', '--path', nargs=1, help='iCloud Keychain Path(~/Library/Keychains/[UUID]/)', required=True)
    parser.add_argument('-k', '--key', nargs=1, help='User Password', required=True)
    parser.add_argument('-x', '--exportfile', nargs=1, help='Write a decrypted contents to SQLite file (optional)', required=False)
    parser.add_argument('-v', '--version', nargs=1, help='macOS version(ex. 10.13)', required=True)

    args = parser.parse_args()

    Pathoficloudkeychain = args.path[0]

    if os.path.isdir(Pathoficloudkeychain) is False:
        print '[!] Path is not directory'
        parser.print_help()
        sys.exit()

    if os.path.exists(Pathoficloudkeychain) is False:
        print '[!] Path is not exists'
        parser.print_help()
        sys.exit()

    # version check
    import re
    gcmIV = ''
    re1='(10)'    # Integer Number 1
    re2='(\\.)' # Any Single Character 1
    re3='(\\d+)'    # Integer Number 2

    rg = re.compile(re1+re2+re3,re.IGNORECASE|re.DOTALL)
    m = rg.match(args.version[0])
    if m:
        minorver = m.group(3)
        if int(minorver) >= 12:
            # Security-57740.51.3/OSX/sec/securityd/SecDbKeychainItem.c:97
            #
            # // echo "keychainblobstaticiv" | openssl dgst -sha256 | cut -c1-24 | xargs -I {} echo "0x{}" | xxd -r | xxd -p  -i
            # static const uint8_t gcmIV[kIVSizeAESGCM] = {
            #     0x1e, 0xa0, 0x5c, 0xa9, 0x98, 0x2e, 0x87, 0xdc, 0xf1, 0x45, 0xe8, 0x24
            # };
            gcmIV = '\x1e\xa0\x5c\xa9\x98\x2e\x87\xdc\xf1\x45\xe8\x24'
        else:
            gcmIV = ''
    else:
        print '[!] Invalid version'
        parser.print_help()
        sys.exit()

    export = 0
    if args.exportfile is not None:

        if os.path.exists(args.exportfile[0]):
            print '[*] Export DB File is exists.'
            sys.exit()
        export = 1

    # Start to analysis
    print 'Tool for iCloud Keychain Analysis by @n0fate'

    MachineUUID = os.path.basename(os.path.normpath(Pathoficloudkeychain))
    PathofKeybag = os.path.join(Pathoficloudkeychain, 'user.kb')
    PathofKeychain = os.path.join(Pathoficloudkeychain, 'keychain-2.db')

    print '[*] macOS version is %s'%args.version[0]
    print '[*] UUID : %s'%MachineUUID
    print '[*] Keybag : %s'%PathofKeybag
    print '[*] iCloud Keychain File : %s'%PathofKeychain

    if os.path.exists(PathofKeybag) is False or os.path.exists(PathofKeychain) is False:
        print '[!] Can not found KeyBag or iCloud Keychain File'
        sys.exit()

    keybag = Keybag(PathofKeybag)
    keybag.load_keybag_header()
    keybag.debug_print_header()

    devicekey = keybag.device_key_init(uuid.UUID(MachineUUID).bytes)
    print '[*] The Device key : %s'%hexlify(devicekey)

    bresult = keybag.device_key_validation()

    if bresult == False:
        print '[!] Device Key validation : Failed. Maybe Invalid PlatformUUID'
        return
    else:
        print '[*] Device Key validation : Pass'

    passcodekey = keybag.generatepasscodekey(args.key[0])

    print '[*] The passcode key : %s'%hexlify(passcodekey)

    keybag.Decryption()

    con = lite.connect(PathofKeychain)
    con.text_factory = str
    cur = con.cursor()
    
    tablelist = ['genp', 'inet', 'cert', 'keys']

    if export:
        # Create DB
        exportDB = ExporySQLiteDB()
        exportDB.createDB(args.exportfile[0])
        print '[*] Export DB Name : %s'%args.exportfile[0]


    for tablename in tablelist:
        if export is not 1:
            print '[+] Table Name : %s'%GetTableFullName(tablename)
        try:
            cur.execute("SELECT data FROM %s"%tablename)
        except lite.OperationalError:
            continue

        if export:
            # Get Table Schema
            sql = con.execute("pragma table_info('%s')"%tablename).fetchall()

            # Create a table
            exportDB.createTable(tablename, sql)

        for data, in cur:
            encblobheader = _memcpy(data[:sizeof(_EncryptedBlobHeader)], _EncryptedBlobHeader)
            encblobheader.clas &= 0x0F

            wrappedkey = data[sizeof(_EncryptedBlobHeader):sizeof(_EncryptedBlobHeader)+encblobheader.length]
            if encblobheader.clas == 11:
                encrypted_data = data[sizeof(_EncryptedBlobHeader)+encblobheader.length:]
                auth_tag = data[-20:-4]
            else:    
                encrypted_data = data[sizeof(_EncryptedBlobHeader)+encblobheader.length:-16]
                auth_tag = data[-16:]

            key = keybag.GetKeybyClass(encblobheader.clas)

            if key == '':
                print '[!] Could not found any key at %d'%encblobheader.clas
                continue

            unwrappedkey = AESUnwrap(key, wrappedkey)

            decrypted = gcm_decrypt(unwrappedkey, gcmIV, encrypted_data, data[:sizeof(_EncryptedBlobHeader)] if gcmIV else '', auth_tag)

            if len(decrypted) is 0:
                #print(" [-] Decryption Process Failed. Invalid Key or Data is corrupted.")
                continue
            
            if export is 0:
                print '[+] DECRYPTED INFO'
            
            blobparse = BlobParser()
            record = blobparse.ParseIt(decrypted, tablename, export)

            if export is 0:
                for k, v in record.items():
                    if k == 'Data':
                        print ' [-]', k
                        hexdump(v)
                    elif k == 'Type' and GetTableFullName(tablename) == 'Keys':
                        print ' [-]', k, ':', blobparse.GetKeyType(int(v))
                    else:
                        print ' [-]', k, ':', v
                print ''
            else:   # export is 1
                record_lst = []
                for k, v in record.items():
                    record_lst.append([k,v])

                exportDB.insertData(tablename, record_lst)

    if export:
        exportDB.commit()
        exportDB.close()

    cur.close()
    con.close()

if __name__ == "__main__":
    main()
