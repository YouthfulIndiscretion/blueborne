import os
from bluetooth import _bluetooth as bt
from pwn import log
import time

src_hci = "hci0"

os.system('sudo hciconfig %src_hci up' %src_hci)
addr = ['%02x' % (ord(c),) for c in os.urandom(6)]
    # NOTW: works only with CSR bluetooth adapters!
os.system('sudo bccmd -d %s psset -r bdaddr 0x%s 0x00 0x%s 0x%s 0x%s 0x00 0x%s 0x%s' %(src_hci, addr[3], addr[5], addr[4], addr[2], addr[1], addr[0]))
final_addr = ':'.join(addr)


time.sleep(10.0)
os.system('sudo hciconfig %src_hci up' %src_hci)
log.info('Set %s to new rand BDADDR %s' % (src_hci, final_addr))
print(bt.hci_devid(final_addr))
