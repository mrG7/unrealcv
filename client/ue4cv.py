'''
UnrealCV
===
Provides functions to interact with games built using Unreal Engine.

>>> import unrealcv
>>> (HOST, PORT) = ('localhost', 9000)
>>> client = unrealcv.Client((HOST, PORT))
'''
import ctypes, struct, threading, socket, re, sys, time, logging
_L = logging.getLogger(__name__)
# L.addHandler(logging.StreamHandler())
_L.addHandler(logging.NullHandler()) # Let client to decide how to do logging
_L.addHandler(logging.StreamHandler()); _L.propagate = False
_L.setLevel(logging.INFO)
# TODO: Add filename

fmt = 'I'

class SocketMessage:
    '''
    Define the format of a message. This class is defined similar to the class FNFSMessageHeader in UnrealEngine4, but without CRC check.
    The magic number is from Unreal implementation
    See https://github.com/EpicGames/UnrealEngine/blob/dff3c48be101bb9f84633a733ef79c91c38d9542/Engine/Source/Runtime/Sockets/Public/NetworkMessage.h
    '''
    magic = ctypes.c_uint32(0x9E2B83C1).value
    def __init__(self, payload):
        self.magic = SocketMessage.magic
        self.payload_size = ctypes.c_uint32(len(payload)).value

    @classmethod
    def ReceivePayload(cls, socket):
        '''
        Return only payload, not the raw message, None if failed
        '''
        # rbufsize = -1 # From SocketServer.py
        rbufsize = 0
        rfile = socket.makefile('rb', rbufsize)
        _L.debug('read raw_magic %s' % threading.current_thread().name)
        try:
            raw_magic = rfile.read(4) # socket is disconnected or invalid
        except Exception as e:
            _L.error('Fail to read raw_magic, %s', e)
            raw_magic = None

        _L.debug('read raw_magic %s done: %s' % (threading.current_thread().name, repr(raw_magic)))
        if not raw_magic: # nothing to read
            # _L.debug('socket disconnect')
            return None
        # print 'Receive raw magic: %d, %s' % (len(raw_magic), raw_magic)
        magic = struct.unpack('I', raw_magic)[0]
        # print 'Receive magic:', magic

        if magic != cls.magic:
            _L.error('Error: receive a malformat message, the message should start from a four bytes uint32 magic number')
            return None
            # The next time it will read four bytes again

        _L.debug('read payload')
        raw_payload_size = rfile.read(4)
        # print 'Receive raw payload size: %d, %s' % (len(raw_payload_size), raw_payload_size)
        payload_size = struct.unpack('I', raw_payload_size)[0]
        _L.debug('Receive payload size %d' % payload_size)

        # if the message is incomplete, should wait until all the data received
        payload = ""
        remain_size = payload_size
        while remain_size > 0:
            data = rfile.read(remain_size)
            if not data:
                return None

            payload += data
            bytes_read = len(data) # len(data) is its string length, but we want length of bytes
            # print 'bytes_read %d, remain_size %d, read_str %s' % (bytes_read, remain_size, data)
            assert(bytes_read <= remain_size)
            remain_size -= bytes_read

        rfile.close()

        return payload

    @classmethod
    def WrapAndSendPayload(cls, socket, payload):
        '''
        Send payload, true if success, false if failed
        '''
        # From SocketServer.py
        # wbufsize = 0, flush immediately
        wbufsize = -1
        # Convert
        socket_message = SocketMessage(payload)
        wfile = socket.makefile('wb', wbufsize)
        # Write the message
        wfile.write(struct.pack(fmt, socket_message.magic))
        # Need to send the packed version
        # print 'Sent ', socket_message.magic

        wfile.write(struct.pack(fmt, socket_message.payload_size))
        # print 'Sent ', socket_message.payload_size

        wfile.write(payload)
        # print 'Sent ', payload
        wfile.flush()
        wfile.close() # Close file object, not close the socket
        return True

class BaseClient:
    '''
    BaseClient send message out and receiving message in a seperate thread.
    After calling the `send` function, only True or False will be returned to indicate whether the operation was successful. If you are trying to send a request and get a response, consider using `Client` instead.
    This class adds message framing on top of TCP
    '''
    def __init__(self, endpoint, message_handler):
        '''
        Parameters:
        endpoint: a tuple (ip, port)
        message_handler: a function defined as `def message_handler(msg)` to handle incoming message, msg is a string
        '''
        self.endpoint = endpoint
        self.message_handler = message_handler
        self.socket = None # if socket == None, means client is not connected
        # self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # self.__isconnected = False
        # self.connect()
        receiving_thread = threading.Thread(target = self.__receiving)
        receiving_thread.setDaemon(1)
        receiving_thread.start()

    def connect(self):
        '''
        Try to connect to server, return whether connection successful
        '''
        if not self.isconnected():
            try:
                # if self.socket: # Create a new socket
                #     self.socket.close()

                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.connect(self.endpoint)
                self.socket = s
                time.sleep(0.1)
                # only assign self.socket to connected socket
                # so it is safe to use self.socket != None to check connection status
                # This does not neccessarily mean connection successful, might be closed by server
                # Unless explicitly to tell the server to accept new socket

                # print 'Ret of connect', ret
                # self.__isconnected = True
                # Start a thread to get data from the socket
            except Exception as e:
                _L.error('Can not connect to %s' % str(self.endpoint))
                _L.error("Error %s", e)
                self.socket = None
                # self.__isconnected = False

        # return self.__isconnected

    def isconnected(self):
        # return self.__isconnected
        # _L.debug(self.socket)
        return self.socket != None

    def disconnect(self):
        # try:
        #     self.socket.shutdown(socket.SHUT_RDWR)
        # except:
        #     pass
        # self.__isconnected = False
        if self.isconnected():
            _L.info("BaseClient, request disconnect from server in %s" % threading.current_thread().name)

            # self.socket.shutdown(socket.SHUT_RDWR)
            self.socket.shutdown(socket.SHUT_RD) # Because socket is on read in __receiving thread, need to call shutdown to force it to close
            if self.socket: # This may also be set in the __receiving thread
                self.socket.close()
                self.socket = None
            time.sleep(0.1) # TODO, this is tricky
        # self.socket.close() # do not release resource, this may be used to connect again

    def __receiving(self):
        '''
        Receive packages, Extract message from packages
        Call self.message_handler if got a message
        Also check whether client is still connected
        '''
        _L.debug('BaseClient start receiving in %s' % threading.current_thread().name)
        while (1):
            if self.isconnected():
                # Only this thread is allowed to read from socket, otherwise need lock to avoid competing
                message = SocketMessage.ReceivePayload(self.socket)
                if not message:
                    # self.__isconnected = False
                    _L.debug('BaseClient disconnected, no more message')
                    self.socket = None
                    continue

                if self.message_handler:
                    self.message_handler(message)
                else:
                    _L.error('No message handler for raw message %s' % message)
                    # TODO: Check error report

    def send(self, message):
        '''
        Send message out, return whether the message was successfully sent
        '''
        if self.isconnected():
            SocketMessage.WrapAndSendPayload(self.socket, message)
            return True
        else:
            _L.error('Fail to send message, client is not connected')
            return False

class Client:
    '''
    Client can be used to send request to a game and get response
    Currently only one client is allowed at a time
    More clients will be rejected
    '''
    def __raw_message_handler(self, raw_message):
        # print 'Waiting for message id %d' % self.message_id
        match = self.raw_message_regexp.match(raw_message)
        if match:
            [message_id, message_body] = (int(match.group(1)), match.group(2)) # TODO: handle multiline response
            message_body = raw_message[len(match.group(1))+1:]
            # print 'Received message id %s' % message_id
            if message_id == self.message_id:
                self.response = message_body
                self.is_timeout = False
                self.wait_response.set()
                self.wait_response.clear() # This is important
            else:
                assert(False)
        else:
            if self.message_handler:
                self.message_handler(raw_message)
            else:
                # Instead of just dropping this message, give a verbose notice
                _L.error('No message handler to handle message %s' % raw_message)

    def __init__(self, endpoint, message_handler=None):
        self.raw_message_regexp = re.compile('(\d{1,8}):(.*)')
        self.message_client = BaseClient(endpoint, self.__raw_message_handler)
        self.message_handler = message_handler
        self.message_id = 0
        self.wait_response = threading.Event()
        self.response = ''
        self.is_timeout = False

        self.isconnected = self.message_client.isconnected
        self.connect = self.message_client.connect
        self.disconnect = self.message_client.disconnect

    # def isconnected(self):
    #     return self.message_client.isconnected()
    #
    # def connect(self):
    #     return self.message_client.connect()
    #
    # def disconnect(self):
    #     return self.message_client.disconnect()

    def request(self, message, timeout=5):
        """
        Send a request to server and wait util get a response from server or timeout.

        Parameters
        ---
        cmd : string, command to control the game
        More info can be seen from http://unrealcv.github.io/commands.html

        Returns
        ---
        response: plain text message from server

        Examples
        ---
        >>> client = Client('localhost', 9000)
        >>> client.connect()
        >>> response = client.request('vget /camera/0/view')
        """
        raw_message = '%d:%s' % (self.message_id, message)
        _L.debug('Request: %s' % raw_message)
        if not self.message_client.send(raw_message):
            return None
        # Timeout is required
        # see: https://bugs.python.org/issue8844
        self.is_timeout = True
        self.wait_response.wait(timeout)
        self.message_id += 1 # Increment it only after the request/response cycle finished

        if self.is_timeout:
            _L.error('Can not receive a response from server, timeout after %d seconds' % timeout)
            return None
        else:
            # print 'Got response +1 id'
            return self.response

(HOST, PORT) = ('localhost', 9000)
client = Client((HOST, PORT), None)
