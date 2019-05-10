#!/usr/bin/env python3

import os, sys, time
import struct
import threading
import json
import array
import binascii
import traceback

from subprocess import run

path = os.environ['HOME_DIR']
sys.path.insert(0, path)

from socket import socket, inet_aton, AF_PACKET, SOCK_RAW, AF_INET
from socket import SOCK_STREAM, SOL_SOCKET, SO_REUSEADDR
from dnx_configure.dnx_system_info import Interface
from dnx_configure.dnx_exceptions import *
from dnx_configure.dnx_packet_checks import Checksums
from tls_proxy.tls_proxy_sniffer import SSLHandlerThread, SSL, SSLType
from tls_proxy.tls_relay_connection_handler import ConnectionHandler as CH


class TLSRelay:
    def __init__(self, action):
        self.action = action
        self.path = os.environ['HOME_DIR']
        
        with open(f'{self.path}/data/config.json', 'r') as settings:
            self.setting = json.load(settings)

        self.lan_int = self.setting['Settings']['Interface']['Inside']
        self.wan_int = self.setting['Settings']['Interface']['Outside']
        self.dnsserver = self.setting['Settings']['DNSServers']

        Int = Interface()
        self.lan_ip = Int.IP(self.lan_int)
        self.wan_ip = Int.IP(self.wan_int)
        dfg = Int.DefaultGateway()
        dfg_mac = Int.IPtoMAC(dfg)
        self.wan_mac = Int.MAC(self.wan_int)
        self.lan_mac = Int.MAC(self.lan_int)
        wan_subnet = Int.WANSubnet(self.wan_int, dfg)
        self.wan_info = [dfg_mac, wan_subnet]

        TLSRelay.connections = {'Clients': {}}
        TLSRelay.active_connections = {'Clients': {}}
        TLSRelay.tcp_handshakes = {'Clients': {}}
        self.nat_ports = {}

        ## RAW Sockets which actually handle the traffic.
        self.lan_sock = socket(AF_PACKET, SOCK_RAW)
        self.lan_sock.bind((self.lan_int, 3))
        self.wan_sock = socket(AF_PACKET, SOCK_RAW)
        self.wan_sock.bind((self.wan_int, 3))

        self.tls_ports = {443}
        self.tcp_info = []
               
    def Start(self):
        self.Main()

    def Main(self):
        print(f'[+] Listening -> {self.lan_int}')
        while True:
            active_connections = self.active_connections['Clients']
            tcp_handshakes = self.tcp_handshakes['Clients']
            conn_handle = False
            relay = True
            data_from_host = self.lan_sock.recv(65565)
            try:
                host_packet_headers = PacketHeaders(data_from_host)
                host_packet_headers.Parse()
            except DNXError as DE:
                pass
            except Exception as E:
                print(f'MAIN PARSE EXCEPTION: {E}')
#                traceback.print_exc()

            if (host_packet_headers.dport in self.tls_ports):
#                    print(f'HTTPS CONNECTION FROM HOST: {host_packet_headers.sport}')
                src_mac = host_packet_headers.smac
                src_ip = host_packet_headers.src
                src_port = host_packet_headers.sport
                dst_ip = host_packet_headers.dst
                dst_port = host_packet_headers.dport
#                    print(f'{active_connections} : {len(host_packet_headers.payload)} || DFH: {len(data_from_host)}')
                if (len(host_packet_headers.payload) == 0):
                    tcp_relay = False
                    if (src_ip not in tcp_handshakes):
                        self.sock, self.connection = self.CreateConnection(src_mac, src_ip, src_port, dst_ip, dst_port)
                        tcp_relay = True
                    elif (src_ip in tcp_handshakes and src_port not in tcp_handshakes[src_ip]):
                        self.sock, self.connection = self.CreateConnection(src_mac, src_ip, src_port, dst_ip, dst_port)
                        tcp_relay = True
                    else:
                        connection = self.connections['Clients'][src_ip][src_port]
                        
                    if (tcp_relay):
                        ConnectionHandler = CH(TLSRelay)
                        TCPRelay = threading.Thread(target=ConnectionHandler.Start, args=('TCP',))
                        TCPRelay.daemon = True
                        TCPRelay.start()
                        
                    packet_from_host = PacketManipulation(host_packet_headers, self.wan_info, data_from_host, connection, from_server=False)
                    packet_from_host.Start()
                    self.wan_sock.send(packet_from_host.send_data)
                    relay = False

                elif (src_ip not in active_connections):
                    nat_port = self.tcp_handshakes['Clients'][src_ip][src_port]
                    active_connections[src_ip] = {src_port: nat_port}
                    conn_handle = True
                elif (src_ip in active_connections and src_port not in active_connections[src_ip]):
                    nat_port = self.tcp_handshakes['Clients'][src_ip][src_port]
                    active_connections[src_ip].update({src_port: nat_port})
                    conn_handle = True
                else:
                    nat_port = active_connections[src_ip][src_port]
                    
                if (relay):
                    connection = self.connections['Clients'][src_ip][src_port]
                    packet_from_host = PacketManipulation(host_packet_headers, self.wan_info, data_from_host, connection, from_server=False)
                    packet_from_host.Start()
                    self.wan_sock.send(packet_from_host.send_data)
                    
                if (conn_handle):
                    SSL = SSLType(data_from_host)
                    _, self.tcp_info = SSL.Parse()
                    print(f'Sending Connection to Thread: CLIENT {src_port} | NAT {nat_port}')
                    ConnectionHandler = CH(TLSRelay)
                    SSLRelay = threading.Thread(target=ConnectionHandler.Start, args=('SSL'))
                    SSLRelay.daemon = True
                    SSLRelay.start()

    def CreateConnection(self, src_mac, src_ip, src_port, dst_ip, dst_port):
        tcp_handshakes = self.tcp_handshakes['Clients']
        connections = self.connections['Clients']
        if (src_ip not in tcp_handshakes):
            sock, nat_port = self.CreateSocket()
            tcp_handshakes[src_ip] = {src_port: nat_port}
            connect = True
        elif (src_ip in tcp_handshakes and src_port not in tcp_handshakes[src_ip]):
            sock, nat_port = self.CreateSocket()
            tcp_handshakes[src_ip].update({src_port: nat_port})
            connect = False
            
        connection = {'Client': {'IP': src_ip, 'Port': src_port, 'MAC': src_mac},
                        'NAT': {'IP': self.wan_ip, 'Port': nat_port, 'MAC': self.wan_mac},
                        'LAN': {'IP': self.lan_ip, 'MAC': self.lan_mac},
                        'Server': {'IP': dst_ip, 'Port': dst_port},
                        'DFG': {'MAC': self.wan_info[0]},
                        'Socket': sock}
                        
        if (connect is True):                
            connections[src_ip] = {src_port: connection}
        elif (connect is False):
            connections[src_ip].update({src_port: connection})

        return sock, connection

    def CreateSocket(self):
        sock = socket(AF_INET, SOCK_STREAM)
        sock.bind((self.wan_ip, 0))
        sock.listen()
        nat_port = sock.getsockname()[1]
                        
        return sock, nat_port

class PacketHeaders:
    def __init__(self, data, nat_port=None):
        self.data = data
        self.nat_port = nat_port

        self.tcp_header_length = 0
        self.dport = None
        self.sport = None

        self.payload = b''

    def Parse(self):
        self.Ethernet()
        self.IP()
        self.Protocol()
        if (self.protocol in {6}):
            self.TCP()
            self.Ports()
            if (self.dport in {443}):
                return
            elif (self.sport in {443} and self.dport == self.nat_port):
                return
            else:
                raise TCPProtocolError('Packet is not related to HTTPS or to the specific session.')
        else:
            raise IPProtocolError('Packet protocol is not 6/TCP')

    def Ethernet(self):
        self.eth_proto = self.data[12:14]
        
        s = []
        smac = self.data[6:12]
        smac = struct.unpack('!6c', smac)
        for byte in smac:
            s.append(byte.hex())
            
        self.smac = f'{s[0]}:{s[1]}:{s[2]}:{s[3]}:{s[4]}:{s[5]}'
    
    ''' Parsing IP headers || SRC and DST IP Address '''
    def IP(self):
        self.ipv4H = self.data[14:34]
        self.checksum = self.ipv4H[10:12]

        src = self.ipv4H[12:16]
        dst = self.ipv4H[16:20]
        s = struct.unpack('!4B', src)
        d = struct.unpack('!4B', dst)        
        self.src = f'{s[0]}.{s[1]}.{s[2]}.{s[3]}'
        self.dst = f'{d[0]}.{d[1]}.{d[2]}.{d[3]}'
        
#        print(f'ORIGINAL SOURCE: {self.src}')
#        print(f'ORIGINAL DESTINATION: {self.dst}')

    def TCP(self):
        bit_values = [32,16,8,4]

        tcp = self.data[34:94]
        tmp_length = bin(tcp[12])[2:6]

        for i, bit in enumerate(tmp_length):
            if (bit == '1'):
                self.tcp_header_length += bit_values[i]

        self.tcp_header = self.data[34:34+self.tcp_header_length]
        self.tcp_length = len(self.data) - 34
        if (len(self.data) > 34+self.tcp_header_length):
            self.payload = self.data[34+self.tcp_header_length:]

    ''' Parsing protocol || TCP 6, UDP 17, etc '''        
    def Protocol(self):
        self.protocol = self.data[23]
        
    ''' Parsing SRC and DST protocol ports '''
    def Ports(self):
        ports = struct.unpack('!2H', self.data[34:38])
        self.sport = ports[0]
        self.dport = ports[1]

        self.src_port = self.data[34:36]

class PacketManipulation:
    def __init__(self, packet_headers, net_info, data, connection, from_server):
        self.Checksum = Checksums()
        self.packet_headers = packet_headers
        self.dst_mac, self.wan_subnet = net_info
        self.data = data
        self.connection = connection
        self.from_server = from_server

        self.tcp_header_length = 0 
        self.dst_ip = None
        self.nat_port = None
        self.payload = b''

        if (from_server):
            self.src_mac = connection['LAN']['MAC']
            self.src_ip = connection['Server']['IP']
            self.dst_ip = connection['Client']['IP']
            self.client_port = connection['Client']['Port']
            self.client_port = struct.pack('!H', connection['Client']['Port'])            
        else:
            self.src_mac = connection['NAT']['MAC']
            self.src_ip = connection['NAT']['IP']
            self.nat_port = connection['NAT']['Port']
            self.nat_port = struct.pack('!H', self.nat_port)
            self.dst_ip = self.packet_headers.dst
            self.dst_port = self.data[36:38]

    def Start(self):
        self.CheckDestination()
        self.TCP()
        self.PsuedoHeader()
        self.RebuildHeaders()

    def CheckDestination(self):
        if (self.dst_ip in self.wan_subnet):
            Int = Interface()
            dst_mac = Int.IPtoMAC(self.dst_ip)
            if (not dst_mac):
                run(f'ping {self.dst_ip} -c 1', shell=True)
                self.dst_mac = Int.IPtoMAC(self.dst_ip)
            else:
                self.dst_mac = dst_mac

    ''' Parsing TCP information like sequence and acknowledgement number amd calculated tcp header
    length to be used by other classes for offset/proper indexing of packet contents.
    Returning all relevant information back to HeaderParse Start method to be redistributed to other classes
    based on need '''
    def TCP(self):
        bit_values = [32,16,8,4]

        tcp = self.data[34:94]
        tmp_length = bin(tcp[12])[2:6]

        for i, bit in enumerate(tmp_length):
            if (bit == '1'):
                self.tcp_header_length += bit_values[i]

        self.tcp_header = self.data[34:34+self.tcp_header_length]
        self.tcp_length = len(self.data) - 34
        if (len(self.data) > 34+self.tcp_header_length):
            self.payload = self.data[34+self.tcp_header_length:]

    def PsuedoHeader(self):
        psuedo_header = b''
        psuedo_header += inet_aton(self.src_ip)
        psuedo_header += inet_aton(self.dst_ip)
        psuedo_header += struct.pack('!2BH', 0, 6, self.tcp_length)
        if (self.from_server):            
            psuedo_header += self.tcp_header[0:2] + self.client_port + self.tcp_header[4:16] + b'\x00\x00' + self.tcp_header[18:]     
        else:
            psuedo_header += self.nat_port + self.tcp_header[2:16] + b'\x00\x00' + self.tcp_header[18:]
        psuedo_packet = psuedo_header + self.payload
        
        tcp_checksum = self.Checksum.TCP(psuedo_packet)
        self.tcp_checksum = struct.pack('<H', tcp_checksum)

    def RebuildHeaders(self):
        ethernet_header = self.RebuildEthernet()
        ip_header = self.RebuildIP()
        tcp_header = self.RebuildTCP()

        self.send_data = ethernet_header + ip_header + tcp_header + self.payload

    def RebuildEthernet(self):
        eth_header = struct.pack('!6s6s',
        binascii.unhexlify(self.dst_mac.replace(':', '')),
        binascii.unhexlify(self.src_mac.replace(':', '')))
        eth_header += self.packet_headers.eth_proto

        return eth_header

    def RebuildIP(self):
        ipv4_header = b''
        ipv4_header += self.packet_headers.ipv4H[:10]
        ipv4_header += b'\x00\x00'
        ipv4_header += inet_aton(self.src_ip)           
        ipv4_header += inet_aton(self.dst_ip) 

        if (len(self.packet_headers.ipv4H) > 20):
            ipv4_header += self.packet_headers.ipv4H[20:]

        ipv4_checksum = self.Checksum.IPv4(ipv4_header)
        ipv4_checksum = struct.pack('<H', ipv4_checksum)
        ipv4_header = ipv4_header[:10] + ipv4_checksum + ipv4_header[12:]

        return ipv4_header

    def RebuildTCP(self):
        if (self.from_server):
            tcp_header = self.tcp_header[:2] + self.client_port + self.tcp_header[4:16] + self.tcp_checksum + b'\x00\x00'
        else:            
            tcp_header = self.nat_port + self.tcp_header[2:16] + self.tcp_checksum + b'\x00\x00'

        if (self.tcp_header_length > 20):
            tcp_header += self.tcp_header[20:self.tcp_header_length]

        return tcp_header