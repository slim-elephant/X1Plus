"""
Module to allow printing using Polar Cloud service.
"""

import asyncio
import aiohttp
import datetime
import logging
import os
import socketio
import ssl
import subprocess
import time
from json import dumps, loads
from jeepney import DBusAddress, new_method_call, MessageType
from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15
from Crypto.Hash import SHA256
from base64 import b64encode

from .dbus import X1PlusDBusService
from x1plus.utils import get_MAC, get_IP, serial_number, is_emulating

logger = logging.getLogger(__name__)

POLAR_INTERFACE = "x1plus.polar"
POLAR_PATH = "/x1plus/polar"

# Setup SSL for aiohttp and websocket.
ssl_ctx = ssl.create_default_context(capath="/etc/ssl/certs")
connector = aiohttp.TCPConnector(ssl=ssl_ctx)
http_session = aiohttp.ClientSession(connector=connector)


class PolarPrintService(X1PlusDBusService):
    def __init__(self, settings, **kwargs):
        """
        The MAC is stored here, but on restart will always generated dynamically
        in an attempt to discourage movement of SD cards.
        """
        self.mac = ""
        # The username can be stored in non-volatile memory, but the PIN must be
        # requested from the interface on every startup.
        self.pin = ""
        # username is here only for emulation mode when reading from .env.
        self.username = ""
        # Todo: Check to see if this is the correct server.
        self.server_url = "https://printer2.polar3d.com"
        self.socket = None
        self.status = 0 # Idle
        self.job_id = ""
        """
        last_ping will hold time of last ping, so we're not sending status
        more than once every 10 seconds.
        """
        self.last_ping = datetime.datetime.now() - datetime.timedelta(seconds = 10)
        self.is_connected = False
        self.ip = ""  # This will be used for sending camera images.
        self.polar_settings = settings
        # status_task_awake is for awaits between status updates to server.
        self.status_task_wake = asyncio.Event()
        # Todo: Fix two "on" fn calls below.
        # self.polar_settings.on("polarprint.enabled", self.sync_startstop())
        # self.polar_settings.on("self.pin", self.set_pin())
        self.socket = None
        super().__init__(
            dbus_interface=POLAR_INTERFACE, dbus_path=POLAR_PATH, **kwargs
        )

    async def task(self) -> None:
        """Create Socket.IO client and connect to server."""
        self.socket = socketio.AsyncClient(http_session=http_session)
        self.set_interface()
        try:
            await self.get_creds()
        except Exception as e:
            logger.debug(f"Polar get_creds: {e}")
            return
        try:
            await self.socket.connect(self.server_url, transports=["websocket"])
        except Exception as e:
            logger.debug(f"Socket connection failed: {e}")
            return
        logger.info("Socket created!")
        # Assign socket callbacks.
        self.socket.on("registerResponse", self._on_register_response)
        self.socket.on("keyPair", self._on_keypair_response)
        self.socket.on("helloResponse", self._on_hello_response)
        self.socket.on("welcome", self._on_welcome)
        self.socket.on("delete", self._on_delete)
        self.socket.on("print", self._on_print)
        self.socket.on("pause", self._on_pause)
        self.socket.on("resume", self._on_resume)
        self.socket.on("cancel", self._on_cancel)
        self.socket.on("delete", self._on_delete)
        await super().task()

    async def unregister(self):
        """
        After a printer is deleted I'll need this in order to reregister it
        Later it'll be necessary for the interface, so I just added it now.
        Todo: In future _on_delete() will remove the username and PIN from
        non-volatile memory (i.e. the SD card), so additional arguments will
        be needed for this to work.
        """
        await self.socket.emit("unregister", self.polar_settings.get("polar.sn"))

    async def _on_welcome(self, response, *args, **kwargs) -> None:
        """
        Check to see if printer has already been registered. If it has, we can
        ignore this. Otherwise, must get a key pair, then call register.
        """
        logger.info("_on_welcome.")
        # Two possibilities here. If it's already registered there should be a
        # Polar Cloud serial number and a set of RSA keys. If not, then must
        # request keys first.
        if self.polar_settings.get("polar.sn", "") and self.polar_settings.get(
            "polar.private_key", ""
        ):
            logger.debug(f"challenge: {response['challenge']}")
            # The printer has been registered.
            # Remember that "polar.sn" is the serial number assigned by the server.
            # First, encode challenge string with the private key.
            private_key = self.polar_settings.get("polar.private_key").encode("utf-8")
            rsa_key = RSA.import_key(private_key)
            hashed_challenge = SHA256.new(response["challenge"].encode("utf-8"))
            key = pkcs1_15.new(rsa_key)
            data = {
                "serialNumber": self.polar_settings.get(
                    "polar.sn"
                ),  # Don't need a default here.
                "signature": b64encode(key.sign(hashed_challenge)).decode("utf-8"),
                "MAC": self.mac,
                "protocol": "2.0",
                "mfgSn": self.serial_number(),
                "printerMake": "Bambu Lab X1 Carbon",
            }
            """
            Note that the following optional fields might be used in future.
            "version": "currently installed software version", // string, optional
            "localIP": "printer's local IP address",           // string, optional
            "rotateImg": 0 | 1,                                // integer, optional
            "transformImg": 0 - 7,                             // integer, optional
            "camOff": 0 | 1,                                   // integer, optional
            "camUrl": "URL for printer's live camera feed"     // string, optional
            """
            await self.socket.emit("hello", data)
        elif not self.polar_settings.get(
            "polar.sn", ""
        ) and not self.polar_settings.get("polar.public_key", ""):
            # We need to get an RSA key pair before we can go further.
            # Todo: This needs to be moved locally rather than being remote, so
            # private key isn't transmitted.
            await self.socket.emit("makeKeyPair", {"type": "RSA", "bits": 2048})
        elif not self.polar_settings.get("polar.sn", ""):
            # We already have a key: just register.
            # Todo: There might be a race condition here or maybe server is sending
            # a lot of welcome requests. Dealt with it by adding self.last_ping.
            logger.info(f"_on_welcome Registering.")
            await self._register()
        else:
            # It's not possible to have a serial number and no key, so this
            # would be a real problem.
            logger.error("Somehow have an SN and no key.")
            exit()

    async def _on_hello_response(self, response, *args, **kwargs) -> None:
        """
        If printer is previously registered, a successful hello response means
        the printer is connected and ready to print.
        """
        if response["status"] == "SUCCESS":
            logger.info("_on_hello_response success")
            logger.info("Polar Cloud connected.")
            self.is_connected = True
        elif response['message'] != "Printer has been deleted":
            logger.error(f"_on_hello_response failure: {response['message']}")
            # Todo: send error to interface.
        await self._status()

    async def _on_keypair_response(self, response, *args, **kwargs) -> None:
        """
        Request a keypair from the server. On success register. On failure kick
        out to interface for new email or pin.
        """
        if response["status"] == "SUCCESS":
            await self.polar_settings.put("polar.public_key", response["public"])
            await self.polar_settings.put("polar.private_key", response["private"])
            # We have keys, but still need to register. First disconnect.
            logger.info("_on_keypair_response success. Disconnecting.")
            # Todo: I'm not creating a race condition with the next three fn calls, am I?
            await self.socket.disconnect()
            self.is_connected = False
            # After the next request the server will respond with `welcome`.
            logger.info("Reconnecting.")
            await self.socket.connect(self.server_url, transports=["websocket"])
            self.is_connected = True
        else:
            # We have an error.
            logger.error(f"_on_keypair_response failure: {response['message']}")
            # Todo: communicate with dbus to fix this!
            # Todo: deal with error using interface.

    async def _on_register_response(self, response, *args, **kwargs) -> None:
        """
        Get register response from status server and save serial number.
        When this fn completes, printer will be ready to receive print calls.
        """
        if response["status"] == "SUCCESS":
            logger.info("_on_register_response success.")
            logger.debug(f"Serial number: {response['serialNumber']}")
            await self.polar_settings.put("polar.sn", response["serialNumber"])
            logger.info("Polar Cloud connected.")

        else:
            logger.error(f"_on_register_response failure: {response['reason']}")
            # Todo: deal with various failure modes here. Most can be dealt
            # with in interface. First three report as server erros? Modes are
            # "SERVER_ERROR": Report this?
            # "MFG_UNKNOWN": Again, should be impossible.
            # "INVALID_KEY": Ask for new key. Maybe have a counter and fail after two?
            # "MFG_MISSING": This should be impossible.
            # "EMAIL_PIN_ERROR": Send it to the interface.
            # "FORBIDDEN": There's an issue with the MAC address.
            if response["reason"].lower() == "forbidden":
                # Todo: Must communicate with dbus to debug this!
                logger.error(
                    f"Forbidden. Duplicate MAC problem!\nTerminating MAC: "
                    f"{self.mac}\n\n"
                )
                return

    async def _register(self) -> None:
        """
        Send register request. Note this can only be called after a keypair
        has been received and stored.
        """
        logger.info("_register.")
        data = {
            "mfg": "bambu",
            "email": self.polar_settings.get("polar.username"),
            "pin": self.pin,
            "publicKey": self.polar_settings.get("polar.public_key"),
            "mfgSn": self.serial_number(),
            "myInfo": {"MAC": self.mac},
        }
        await self.socket.emit("register", data)

    async def _status_update(self):
        """
        Codes from the printer:
        TaskStage: We'll ignore this; just here for completeness.
          0: INITING
          1: WAITING
          2: WORKING
          3: PAUSED
        TaskState
          0: IDLE
          1: SLICING
          2: PREPARE
          3: RUNNING
          4: FINISH
          5: FAILED
          6: PAUSE

        Codes to return to the server:
        0     Ready; printer is idle and ready to print
        1     Serial; printer is printing a local print over its serial connection
        2     Preparing; printer is preparing a cloud print (e.g., slicing)
        3     Printing; printer is printing a cloud print
        4     Paused; printer has paused a print
        5     Postprocessing; printer is performing post-printing operations
        6     Canceling; printer is canceling a print from the cloud
        7     Complete; printer has completed a print job from the cloud
        8     Updating; printer is updating its software
        9     Cold pause; printer is in a "cold pause" state
        10    Changing filament; printer is in a "change filament" state
        11    TCP/IP; printer is printing a local print over a TCP/IP connection
        12    Error; printer is in an error state
        13    Disconnected; controller's USB is disconnected from the printer
        14    Door open; unable to start or resume a print
        15    Clear build plate; unable to start a new print

        Several of these output states will be ignored for now.
        Todo: 15 **really, really** needs to be dealt with.
        """

        """ Sample code, but see this link:
        https://github.com/X1Plus/X1Plus/commit/b0495c528d1a05e76f7ce6ffc00aa57e5e6d2a98?diff=unified&w=0#diff-6528af1f39b80d6774161464e2c4a65a16e2b72e5ff2b2cf79662b040aea0550R75-R88

        Joshua: I believe object path should
        be bbl.service.screen,
        bus name /bbl/service/screen,
        interface bbl.screen.x1plus

        try:
            addr = DBusAddress("/x1plus/polar", bus_name="x1plus.polar", interface="x1plus.polar")
            method = "getStatus"
            params =
        except Exception as e:
            # deal with this
        # Get a json string back.
        msg = new_method_call(addr, method, 'x', body=())

        Return fn from .js:
        See Ping fn to see how this should actually be done.
        // Return a json string.
        function checkNow() {
            return '{"stage": "' + X1Plus.curTask.stage + '", "state": "' + X1Plus.curTask.state + '"}';
        }

        Also see here: https://github.com/X1Plus/X1Plus/blob/main/bbl_screen-patch/patches/printerui/qml/X1Plus.js#L167-L170
        """

        """
        status_translation: {
            "0": 0, # Idle and ready
            "1": 2, #
            "2": 2,
            "3": 3,
            "4": 7,
            "5": 5,
            "6": 4,
        }
        rv = {'jsonrpc': '2.0', 'id': 1}
        try:
            addr = DBusAddress("bbl.service.screen", bus_name="/bbl/service/screen", interface="bbl.screen.x1plus")
            method = "getStatus"
            params = {"text": "getStatus"}
        except Exception as e:
            logger.error(f"Something failed in status call: {e}")
            return
        dbus_msg = new_method_call(addr, method, 's', (dumps(params), ))
        reply = await self.router.send_and_get_reply(dbus_msg)
        if reply.header.message_type == MessageType.error:
            rv['error'] = {'code': 1, 'message': reply.header.fields.get(HeaderFields.error_name, 'unknown-error') }
        else:
            rv['result'] = loads(reply.body[0])
        # except Exception as e:
        #     # pkt['error'] = {'code': -32602, 'message': str(e)}
        #     await ws.send_json(rv)
        #     # continue
        dmsg = new_method_call(addr, method, 's', (dumps(params), ))
        reply = await self.router.send_and_get_reply(dmsg)
        if reply.header.message_type == MessageType.error:
            rv['error'] = {'code': 1, 'message': reply.header.fields.get(HeaderFields.error_name, 'unknown-error') }
        else:
            rv['result'] = loads(reply.body[0])
        logger.info(f"**** result: {rv['result']}")
        """

        # This is a total ugly hack.
        dbus_call = ["dbus-send", "--system", "--print-reply", "--dest=bbl.service.screen", "/bbl/service/screen", "bbl.screen.x1plus.getStatus", 'string: {"text": "hello"}']
        stage_and_state = subprocess.run(dbus_call, capture_output=True).stdout.strip()
        # should actually decode next line, but I'm in a hurry.
        stage_and_state_json = str(stage_and_state).split("string ")[1][1:-2]
        output = loads(stage_and_state_json)
        task_state = output["state"]
        task_stage = output["stage"]
        logger.debug(f"  *** job id: {self.job_id}, stage: {task_stage}, status: {task_state}")
        self.status = 0
        if task_state > 0 and task_state < 4:
            if not self.job_id:
                self.status = 1
            else:
                self.status = 3
        elif self.status == 6:
            await self._job("canceled")
            self.status = 6
        elif self.status == 7:
            await self._job("completed")


    async def _status(self) -> None:
        """
        Should send several every 10 seconds. All fields but serialNumber
        and status are optional.
        {
            "serialNumber": "string",
            "status": integer,
            "progress": "string",
            "progressDetail": "string",
            "estimatedTime": integer,
            "filamentUsed": integer,
            "startTime": "string",
            "printSeconds": integer,
            "bytesRead": integer,
            "fileSize": integer,
            "tool0": floating-point,
            "tool1": floating-point,
            "bed": floating-point,
            "chamber": floating-point,
            "targetTool0": floating-point,
            "targetTool1": floating-point,
            "targetBed": floating-point,
            "targetChamber": floating-point,
            "door": integer,
            "jobId": "string",
            "file": "string",
            "config": "string"
        }
        """
        while True:
            await self._status_update()
            # self.polar_settings.on("status", lambda:self._status_update)
            if not self.is_connected:
                return
            if (datetime.datetime.now() - self.last_ping).total_seconds() <=5:
                # No extra status updates.
                return
            data = {
                "serialNumber": self.polar_settings.get("polar.sn"),
                "status": self.status,
            }
            try:
                await self.socket.emit("status", data)
                logger.debug(f"status data {data}")
                logger.info(f"Status update {datetime.datetime.now()}")
                self.last_ping = datetime.datetime.now()
            except Exception as e:
                logger.error(f"emit status failed: {e}")
                if str(e) == "/ is not a connected namespace.":
                    logger.debug('Got "/ is not a connected namespace." error.')
                    # This seems to be a python socketio bug/feature?
                    # In any case, recover by reconnecting.
                    await self.socket.disconnect()
                    logger.info("Disconnecting.")
                    self.is_connected = False
                    # After the next request the server will respond with `welcome`.
                    logger.info("Reconnecting.")
                    await self.socket.connect(self.server_url, transports=["websocket"])
                    self.is_connected = True
                    return # Or else we'll starting sending too many updates.
            await asyncio.sleep(10)
            # try:
            #     await asyncio.wait_for(self.status_task_wake.wait(), timeout=50)
            # except TimeoutError as e:
            #     logger.error(e)
            # self.status_task_wake.clear()
        logger.debug("Status ending.")

    async def _on_delete(self, response, *args, **kwargs) -> None:
        """
        Printer has been deleted from Polar Cloud. Remove all identifying information
        from card. The current print should finish. Disconnect socket so that
        username and PIN don't keep being requested and printer doesn't reregister.
        """
        logger.info("_on_delete")
        if response["serialNumber"] == self.polar_settings.get("polar.sn"):
            self.pin = ""
            self.mac = ""
            self.username = ""
            to_remove = {
                # "polar.sn", "", # Todo: add this back in after interface is done.
                "polar.username": "",
                "polar.public_key": "",
                "polar.private_key": "",
            }
            await self.polar_settings.put_multiple(to_remove)
            await self.socket.disconnect()
            self.is_connected = False

    async def get_creds(self) -> None:
        """
        If PIN and username are not set, open Polar Cloud interface window and
        get them.
        Todo: This works only during emulation.
        """
        if is_emulating():
            # I need to use actual account creds to connect, so we're using .env
            # for testing, until there's an interface.
            # dotenv isn't installed, so just open the .env file and parse it.
            # This means that .env file must formatted correctly, with var names
            # `username` and `pin`.
            from pathlib import Path
            env_file = Path(__file__).resolve().parents[0] / ".env"
        else:
            env_file = os.path.join("/sdcard", ".env")
            if not self.pin:
                # Get it from the interface.
                pass
            if not self.polar_settings.get("polar.username", ""):
                # Get it from the interface.
                pass
        with open(env_file) as env:
            for line in env.readlines():
                k, v = line.strip().split("=")
                if k == "username":
                    self.username = v
                elif k == "pin":
                    self.pin = v
        await self.polar_settings.put("polar.username", self.username)

    async def _job(self, status):
        logger.info(f"_job {status} {self.job_id}")
        data = {
            "serialNumber": self.polar_settings.get("polar.sn"),
            "jobId": self.job_id,
            "state": status, # "completed" | "canceled"
            # Next two when we get more features implemented
            # "printSeconds": integer,              // integer, optional
            # "filamentUsed": integer               // integer, optional
        }
        await self.socket.emit("job", data)

    async def _on_print(self, data, *args, **kwargs):
        logger.debug("_on_print")
        self.job_id = data["jobId"]
        if not data["serialNumber"] or data["serialNumber"] != data.get("serialNumber", ""):
            logger.debug("Serial numbers don't match.")
            await self._job("canceled")
            return
        # Todo: add recovery here if still printing.

        print_file = ''
        if 'gcodeFile' not in data:
            logger.error("PolarCloud sent non-gcode file.")
            await self._job("canceled")
            return

        path = "/tmp/x1plus" if is_emulating() else "/sdcard"
        await self._download_file(path, f"{data['jobName']}.gcode", data["gcodeFile"])
        # try:
        #     info['file'] = print_file
        #     req_stl = requests.get(print_file, timeout=5)
        #     req_stl.raise_for_status()
        # except Exception:
        #     logger.exception(f"Could not retrieve print file from PolarCloud: {print_file}")
        #     await self._job("canceled")
        #     return


        self.job_id = ""
        # self._file_manager.add_file(FileDestinations.LOCAL, path, StreamWrapper(path, BytesIO(req_stl.content)), allow_overwrite=True)
        # self.job_id = data['jobId'] if 'jobId' in data else "123"
        # logger.debug(f"print jobId is {self.jobId}")
        # logger.debug(f"print data is {data}")

        # if self._printer.is_closed_or_error():
        #     self._printer.disconnect()
        #     self._printer.connect()

        # self._cloud_print = True
        # await self._job_pending = True
        # self._job_id = job_id
        # self._pstate_counter = 0
        # self._pstate = self.PSTATE_PREPARING
        # self._cloud_print_info = info
        # self._status_now = True

    def dbus_PrintFile(self, file_name):

        pass

    async def _download_file(self, path, file, url):
        """Adapted/stolen from ota.py. Maybe could move to utils?"""
        try:
            try:
                os.mkdir(path)
            except:
                logger.info(f"_download_file. {path} already exists.")
            dest = os.path.join(path, file)
            logger.info("_download_file")
            logger.debug(f"downloading {url} to {dest}")
            download_bytes = 0
            download_bytes_total = -1
            with open(dest, 'wb') as f:
                logger.debug("Opened file to write.")
                timeout = aiohttp.ClientTimeout(connect=5, total=900, sock_read=10)
                async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_ctx), timeout=timeout) as session:
                    async with session.get(url) as response:
                        response.raise_for_status()
                        self.download_bytes_total = int(response.headers['content-length'])
                        self.download_bytes = 0
                        last_publish = datetime.datetime.now()
                        async for chunk in response.content.iter_chunked(131072):
                            logger.debug("Downloaded a chunk.")
                            self.download_bytes += len(chunk)
                            f.write(chunk)
        except:
            try:
                os.unlink(dest)
            except:
                pass
            raise
        logger.info(f"_download_file success: {os.path.getsize(dest)}")

    async def _on_pause(self):
        self.status = 4
        pass

    async def _on_resume(self):
        pass

    async def _on_cancel(self):
        self.status = 6
        pass


    def set_interface(self) -> None:
        """
        Get IP and MAC addresses and store them in self.settings. This is
        intentionally dynamic as a security measure.
        """
        self.mac = get_MAC()
        self.ip = get_IP()

    def serial_number(self) -> str:
        """
        Return the Bambu serial number—NOT the Polar Cloud SN. If emulating,
        random string.
        """
        if is_emulating:
            return "123456789"
        else:
            return serial_number()
