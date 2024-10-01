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
from json import loads
from jeepney import DBusAddress, new_method_call, MessageType
from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15
from Crypto.Hash import SHA256
from base64 import b64encode

from .dbus import X1PlusDBusService
from x1plus.utils import get_IP, get_MAC, is_emulating
from x1plus.utils import serial_number as utils_sn

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("/var/log/polar_debug.log"),
        logging.StreamHandler(),
    ],
)

POLAR_INTERFACE = "x1plus.polar"
POLAR_PATH = "/x1plus/polar"

# Setup SSL for aiohttp and websocket.
ssl_ctx = ssl.create_default_context(capath="/etc/ssl/certs")
connector = aiohttp.TCPConnector(ssl=ssl_ctx)
http_session = aiohttp.ClientSession(connector=connector)
aws_connector = aiohttp.TCPConnector(ssl=ssl_ctx)
aws_http_session = aiohttp.ClientSession(connector=aws_connector)


class PolarPrintService(X1PlusDBusService):
    def __init__(self, router, daemon, **kwargs):
        """
        The MAC is stored here, but on restart will always generated dynamically
        in an attempt to discourage movement of SD cards.
        """
        logger.info("Polar __init__.")
        self.daemon = daemon
        self.mac = ""
        # The username can be stored in non-volatile memory, but the PIN must be
        # requested from the interface on every startup.
        self.pin = ""
        # username is here only for emulation mode when reading from `env`.
        # Note that we're using env and not .env so users can easily edit it.
        self.username = ""
        # Todo: Check to see if this is the correct server.
        self.server_url = "https://printer2.polar3d.com"
        self.socket = None
        self.status = 0  # Idle
        self.job_id = "123"  # Defaults to serial if Polar Cloud hasn't sent anything.
        """
        self.downloading will allow me to update status when printer doesn't realize
        it's busy.
        """
        self.downloading = False
        """
        self.downloading will allow me to update status when printer doesn't realize
        it's busy.
        """
        self.downloading = False
        """
        last_ping will hold time of last ping, so we're not sending status
        more than once every 10 seconds.
        """
        self.last_ping = datetime.datetime.now() - datetime.timedelta(seconds=10)
        self.is_connected = False
        self.ip = ""  # This will be used for sending camera images.
        # status_task_awake is for awaits between status updates to server.
        self.status_task_wake = asyncio.Event()
        # Todo: Fix two "on" fn calls below.
        # self.daemon.settings.on("polarprint.enabled", self.sync_startstop())
        # self.daemon.settings.on("self.pin", self.set_pin())
        self.socket = None
        # status_lookup will be used in _status_update() to interpret job states
        # coming from mqtt.
        self.status_lookup = {
            "IDLE": 0,
            "SLICING": 1,
            "PREPARE": 2,
            "RUNNING": 3,
            "FINISH": 4,
            "FAILED": 5,
            "PAUSE": 6,
        }
        self.start_time = datetime.datetime.now() # Start time for each print job.
        # How long the job will take in seconds. This is set in _status_() and
        # reset in _job().
        self.estimated_print_time = 0
        """
        Next two variables are for sending camera images to Polar Cloud. None of
        this should be persistent, especially the urls, since they expire, so all
        saved as instance vars. The first set is for sending images while printing,
        the second set for printing when idle.
        """
        self.printing_cam_stream = {"expiration_time": time.time(), "form_data": dict(), "curl_call": ""}
        self.idle_cam_stream = {"expiration_time": time.time(), "form_data": dict(), "curl_call": ""}
        super().__init__(
            router=router,
            dbus_interface=POLAR_INTERFACE,
            dbus_path=POLAR_PATH,
            **kwargs,
        )

    async def task(self) -> None:
        """Create Socket.IO client and connect to server."""
        logger.info("Polar task")
        # Set socketio to use Python's logger object.
        self.socket = socketio.AsyncClient(
            http_session=http_session, logger=logger, engineio_logger=logger
        )
        self._set_interface()
        try:
            await self._get_creds()
        except Exception as e:
            logger.debug(f"Polar _get_creds failed: {e}")
            return
        time.sleep(45)
        try:
            await self.socket.connect(
                self.server_url, transports=["websocket"], wait_timeout=10
            )
        except Exception as e:
            logger.debug(f"Polar socket connection failed: {e}")
            return
        logger.info("Polar socket created!")
        await self._get_url("idle")
        await self._get_url("printing")
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
        self.socket.on("getUrlResponse", self._on_url_response)
        await super().task()
        logger.info("Polar Cloud is running.")

    async def _unregister(self):
        """
        After a printer is deleted I'll need this in order to reregister it
        Later it'll be necessary for the interface, so I just added it now.
        Todo: In future _on_delete() will remove the username and PIN from
        non-volatile memory (i.e. the SD card), so additional arguments will
        be needed for this to work.
        """
        await self.socket.emit("unregister", self.daemon.settings.get("polar.sn"))

    async def _on_welcome(self, response, *args, **kwargs) -> None:
        """
        Check to see if printer has already been registered. If it has, we can
        ignore this. Otherwise, must get a key pair, then call register.
        """
        logger.info("Polar _on_welcome.")
        # Two possibilities here. If it's already registered there should be a
        # Polar Cloud serial number and a set of RSA keys. If not, then must
        # request keys first.
        if self.daemon.settings.get("polar.sn", "") and self.daemon.settings.get(
            "polar.private_key", ""
        ):
            logger.debug(f"Polar challenge: {response['challenge']}")
            # The printer has been registered.
            # Remember that "polar.sn" is the serial number assigned by the server.
            # First, encode challenge string with the private key.
            private_key = self.daemon.settings.get("polar.private_key").encode("utf-8")
            rsa_key = RSA.import_key(private_key)
            hashed_challenge = SHA256.new(response["challenge"].encode("utf-8"))
            key = pkcs1_15.new(rsa_key)
            data = {
                "serialNumber": self.daemon.settings.get(
                    "polar.sn"
                ),  # Don't need a default here.
                "signature": b64encode(key.sign(hashed_challenge)).decode("utf-8"),
                "MAC": self.mac,
                "protocol": "2.0",
                "mfgSn": self._serial_number(),
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
        elif not self.daemon.settings.get(
            "polar.sn", ""
        ) and not self.daemon.settings.get("polar.public_key", ""):
            # We need to get an RSA key pair before we can go further.
            # Todo: This needs to be moved locally rather than being remote, so
            # private key isn't transmitted.
            await self.socket.emit("makeKeyPair", {"type": "RSA", "bits": 2048})
        elif not self.daemon.settings.get("polar.sn", ""):
            # We already have a key: just register.
            # Todo: There might be a race condition here or maybe server is sending
            # a lot of welcome requests. Dealt with it by adding self.last_ping.
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
            logger.info("Polar _on_hello_response success")
            logger.info("Polar Cloud connected.")
            self.is_connected = True
        elif response["message"] != "Printer has been deleted":
            logger.error(f"_on_hello_response failure: {response['message']}")
            # Todo: send error to interface.
        await self._status()

    async def _on_keypair_response(self, response, *args, **kwargs) -> None:
        """
        Request a keypair from the server. On success register. On failure kick
        out to interface for new email or pin.
        """
        if response["status"] == "SUCCESS":
            await self.daemon.settings.put("polar.public_key", response["public"])
            await self.daemon.settings.put("polar.private_key", response["private"])
            # We have keys, but still need to register. First disconnect.
            logger.info("Polar _on_keypair_response success. Disconnecting.")
            # Todo: I'm not creating a race condition with the next three fn calls, am I?
            await self.socket.disconnect()
            self.is_connected = False
            # After the next request the server will respond with `welcome`.
            logger.info("Polar Reconnecting.")
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
            logger.info("Polar _on_register_response success.")
            logger.debug(f"Polar serial number: {response['serialNumber']}")
            await self.daemon.settings.put("polar.sn", response["serialNumber"])
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
        logger.info("Polar _register.")
        data = {
            "mfg": "bambu",
            "email": self.daemon.settings.get("polar.username"),
            "pin": self.pin,
            "publicKey": self.daemon.settings.get("polar.public_key"),
            "mfgSn": self._serial_number(),
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
        0   Ready; printer is idle and ready to print
        1   Serial; printer is printing a local print over its serial connection
        2   Preparing; printer is preparing a cloud print (e.g., slicing)
        3   Printing; printer is printing a cloud print
        4   Paused; printer has paused a print
        5   Postprocessing; printer is performing post-printing operations
        6   Canceling; printer is canceling a print from the cloud
        7   Complete; printer has completed a print job from the cloud
        8   Updating; printer is updating its software
        9   Cold pause; printer is in a "cold pause" state
        10  Changing filament; printer is in a "change filament" state
        11  TCP/IP; printer is printing a local print over a TCP/IP connection
        12  Error; printer is in an error state
        13  Disconnected; controller's USB is disconnected from the printer
        14  Door open; unable to start or resume a print
        15  Clear build plate; unable to start a new print

        Several of these output states will be ignored for now.
        Todo: 15 **really, really** needs to be dealt with.
        """
        task_state = self.status_lookup[self.daemon.mqtt.latest_print_status.get("gcode_state", "IDLE")]
        task_stage = self.daemon.mqtt.latest_print_status.get("mc_print_stage", )
        logger.debug(
            f"Polar  *** job id: {self.job_id}, stage: {task_stage}, status: {task_state}"
        )
        self.status = 0
        if task_state > 0 and task_state < 4:
            if self.job_id == "123":
                self.status = 1
            else:
                self.status = 3
        elif task_state == 4:
            # Finished
            await self._job("completed")
            self.status == 0
        elif task_state == 5:
            # Failed
            if self.status != 0:
                await self._job("canceled")
                self.status = 0
        elif task_state == 6:
            # Paused
            # await self._job("canceled")
            self.status = 4

    async def _status(self) -> None:
        """
        Send a status message every 10 seconds. Also trigger sending of image
        every 20 seconds.

        All fields but serialNumber and status are optional.
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
        capture_image = True
        while True:
            await self._status_update()
            if not self.is_connected:
                return
            if self.daemon.mqtt.latest_print_status.get("mc_percent", 100) < 5:
                # At this point we can guess the total print time. It'll possibly
                # be off by a little. But the mqtt doesn't have a value for total
                # time that I can find.
                self.estimated_print_time = self.daemon.mqtt.latest_print_status.get('mc_remaining_time', 0)
            if (datetime.datetime.now() - self.last_ping).total_seconds() <= 5:
                # Don't do extra status updates.
                return
            data = {
                "tool0": self.daemon.mqtt.latest_print_status.get("nozzle_temper", 0),
                "bed": self.daemon.mqtt.latest_print_status.get("bed_temper", 0),
                "chamber": self.daemon.mqtt.latest_print_status.get("chamber_temper", 0),
                "targetTool0": self.daemon.mqtt.latest_print_status.get("nozzle_target_temper", 0),
                "targetBed": self.daemon.mqtt.latest_print_status.get("bed_target_temper", 0),
                # "targetChamber": self.daemon.mqtt.latest_print_status.get("", "0"), # isn't in mqtt?
                "serialNumber": self.daemon.settings.get("polar.sn"),
                "status": self.status,
                "startTime": self.start_time.isoformat(),
                "estimatedTime": self.estimated_print_time,
                "printSeconds": (datetime.datetime.now() - self.start_time).total_seconds()
            }
            try:
                await self.socket.emit("status", data)
                logger.info(
                    f"Polar status update {self.status} {datetime.datetime.now()}"
                )
                self.last_ping = datetime.datetime.now()
                # Todo: turn capture_image back on.
                # if capture_image == True:
                #     # Capture and upload a new image every other status update.
                #     self.capture_frames(
                #         device_path="/dev/video20",
                #         output_dir="ipcam",
                #         num_frames=1,
                #         interval=1,
                #     )
                #     # Todo: turn upload jpeg back on.
                #     # if self.status in [0, 8, 9, 10, 15]:
                #     #     # In these cases send idle image.
                #     #     # (Todo: are these all the cases for idle?)
                #     #     await self._upload_jpeg("idle")
                #     # elif self.status != 11:
                #     #     await self._upload_jpeg("printing")
                # # Next time through loop it will capture (or not).
                # capture_image = not capture_image

            except Exception as e:
                logger.error(f"emit status failed: {e}")
                if str(e) == "/ is not a connected namespace.":
                    logger.debug('Polar got "/ is not a connected namespace." error.')
                    # This seems to be a python socketio bug/feature?
                    # In any case, recover by reconnecting.
                    logger.info("Polar disconnecting.")
                    await self.socket.disconnect()
                    self.is_connected = False
                    # After the next request the server will respond with `welcome`.
                    logger.info("Polar reconnecting.")
                    await self.socket.connect(self.server_url, transports=["websocket"])
                    self.is_connected = True
                    return  # Or else we'll starting sending too many updates.

            await asyncio.sleep(10)
        logger.debug("Polar status ending.")

    async def _upload_jpeg(self, which_state="printing") -> None:
        """
        Upload an image to Polar Cloud's S3. The url varies for idle or printing
        images. All the connection info is stored in self.printing_cam_stream
        and self.idle_cam_stream.
        """
        logger.info("Polar _upload_jpeg")
        if which_state not in ["idle", "printing"]:
            # We can't send images. Todo: Fail ugly for now.
            return
        if which_state == "idle":
            this_vars = self.idle_cam_stream
        else:
            this_vars = self.printing_cam_stream
        if this_vars["expiration_time"] - time.time() < 300:
            # There are fewer than five mins before the upload url expires;
            # get a new one.
            await self._get_url(which_state)
        # Now do actual upload.
        # logger.debug(f"Polar AWS form data {this_vars['form_data']}")
        # logger.debug(f"Polar curl call {this_vars['curl_call']}")
        response_data = subprocess.run(this_vars["curl_call"], capture_output=True, shell=True).stdout.strip()
        # async with aws_http_session.post(this_vars["url"], data=this_vars["form_data"]) as response:
        #     # Process the response
        #     response_data = await response.text()
        logger.debug(f"Attempted image uploaded\n{response_data}")

    async def _on_delete(self, response, *args, **kwargs) -> None:
        """
        Printer has been deleted from Polar Cloud. Remove all identifying information
        from card. The current print should finish. Disconnect socket so that
        username and PIN don't keep being requested and printer doesn't reregister.
        """
        logger.info("Polar _on_delete")
        if response["serialNumber"] == self.daemon.settings.get("polar.sn"):
            self.pin = ""
            self.mac = ""
            self.username = ""
            to_remove = {
                # "polar.sn", "", # Todo: add this back in after interface is done.
                "polar.username": "",
                "polar.public_key": "",
                "polar.private_key": "",
            }
            await self.daemon.settings.put_multiple(to_remove)
            await self.socket.disconnect()
            self.is_connected = False

    async def _get_creds(self) -> None:
        """
        If PIN and username are not set, open Polar Cloud interface window and
        get them.
        Todo: This works only during emulation.
        """
        logger.info("Polar _get_creds")
        if is_emulating():
            # I need to use actual account creds to connect, so we're using env
            # for testing, until there's an interface. Not it's env and not .env
            # so users can more easily edit the file.
            # Just open the env file and parse it.
            # This means that env file must formatted correctly, with var names
            # `username` and `pin`.
            from pathlib import Path

            env_file = Path(__file__).resolve().parents[0] / "env"
        else:
            env_file = os.path.join("/sdcard", "env")
            if not self.pin:
                # Get it from the interface.
                pass
            if not self.daemon.settings.get("polar.username", ""):
                # Get it from the interface.
                pass
        # For now must use .env. eventually kill this.
        with open(env_file) as env:
            for line in env.readlines():
                k, v = line.strip().split("=")
                if k == "username":
                    self.username = v
                elif k == "pin":
                    self.pin = v
        await self.daemon.settings.put("polar.username", self.username)

    async def _job(self, status):
        """
        Send job response to Polar Cloud, letting it know a job has finished.
        Also reset job_id to "123", which is the default.
        """
        logger.info(f"Polar _job {status} {self.job_id}")
        data = {
            "serialNumber": self.daemon.settings.get("polar.sn"),
            "jobId": self.job_id,
            "state": status,  # "completed" | "canceled"
            # Next two when we get more features implemented
            # "printSeconds": integer,              // integer, optional
            # "filamentUsed": integer               // integer, optional
        }
        self.start_time = datetime.datetime.now()
        self.estimated_print_time = 0
        # I can reset job_id here because I only call _job() when a job
        # has completed.
        self.job_id = "123"
        await self.socket.emit("job", data)

    async def _on_print(self, data, *args, **kwargs):
        """Download file to printer, then send to print."""
        logger.info("Polar _on_print")
        self.job_id = data["jobId"]
        if not data["serialNumber"] or data["serialNumber"] != data.get(
            "serialNumber", ""
        ):
            logger.debug("Polar serial numbers don't match.")
            await self._job("canceled")
            return
        # Todo: add recovery here if still printing.

        logger.info("")
        logger.info(f"*** Polar print incoming data: {data}")
        logger.info("")
        path = "/tmp/x1plus" if is_emulating() else "/sdcard"
        file_name = data["jobName"]
        await self._download_file(path, file_name, data["gcodeFile"])
        location = os.path.join(path, file_name)
        self.start_time = datetime.datetime.now()
        self._printer_action("gcode_file", location)

    async def _download_file(self, path, file, url):
        """Adapted/stolen from ota.py. Maybe could move to utils?"""
        try:
            try:
                os.mkdir(path)
            except:
                logger.error(f"Polar _download_file. {path} already exists.")
            dest = os.path.join(path, file)
            logger.info("Polar _download_file")
            logger.debug(f"Polar downloading {url} to {dest}")
            download_bytes = 0
            download_bytes_total = -1
            with open(dest, "wb") as f:
                logger.info("Polar opened file to write.")
                timeout = aiohttp.ClientTimeout(connect=5, total=900, sock_read=10)
                async with aiohttp.ClientSession(
                    connector=aiohttp.TCPConnector(ssl=ssl_ctx), timeout=timeout
                ) as session:
                    async with session.get(url) as response:
                        response.raise_for_status()
                        self.download_bytes_total = int(
                            response.headers["content-length"]
                        )
                        self.download_bytes = 0
                        last_publish = datetime.datetime.now()
                        async for chunk in response.content.iter_chunked(131072):
                            self.downloading = True
                            self.download_bytes += len(chunk)
                            f.write(chunk)
            self.downloading = False
        except:
            try:
                os.unlink(dest)
            except:
                pass
            raise
        logger.info(f"Polar _download_file success: {os.path.getsize(dest)}")

    async def _on_pause(self, data, *args, **kwargs) -> None:
        logger.info("Polar _on_pause")
        self._printer_action("pause")

    async def _on_resume(self, data, *args, **kwargs) -> None:
        logger.info("Polar _on_resume")
        self._printer_action("resume")

    async def _on_cancel(self, data, *args, **kwargs) -> None:
        logger.info("Polar _on_cancel")
        self._printer_action("stop")

    async def _on_url_response(self, data, *args, **kwargs) -> None:
        """
        Upon receiving url response set appropriate instance variables. Response
        object is as follows:

        {
            "serialNumber": "printer-serial-number",
            "type": "idle" | "printing" | "timelapse",
            "expires": seconds-until-url-expires,
            "maxSize": max-file-size-in-bytes,
            "contentType": "image/jpeg" | "video/mp4",
            "method": "post",
            "url": "string",
            "fields": { // POST form data
                "bucket": "S3-bucket-name",
                "key": "bucket-key-name",
                "acl": "public-read",
                "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
                "X-Amz-Credential": "AWS-credential",
                "X-Amz-Date": "time-stamp",
                "Policy": "encrypted-policy",
                "X-Amz-Signature": "policy-signature"
            }
        }
        """
        logger.info("Polar _on_url_response")
        if data["status"] == "SUCCESS" and data[
            "serialNumber"
        ] == self.daemon.settings.get("polar.sn", ""):
            if data["type"] == "idle":
                cam_dict = self.idle_cam_stream
            elif data["type"] == "printing":
                cam_dict = self.printing_cam_stream
            cam_dict["expiration_time"] = time.time() + int(data["expires"])
            # cam_dict["url"] = data["url"]
            # cam_dict["form_data"] = data["fields"].copy()
            # cam_dict["form_data"]["file"] = "@/sdcard/ipcam/frame_0.jpg;type=image/jpeg"
            # cam_dict["form_data"]["X-Amz-Expires"] = data["expires"]
            # curl_form_string = " ".join([f"-F '{x}={y}'" for (x, y) in data["fields"].items()])
            curl_list = ["curl", "-X", "POST"]
            for k, v in data["fields"].items():
                curl_list += ["-F", f"'{k}={v}'"]
            curl_list += ["-F", "'file=@/sdcard/ipcam/frame_0.jpg;type=image/jpeg'"]
            curl_list.append(f"'{data["url"]}'")
            cam_dict["curl_call"] = " ".join(curl_list)
        # Todo: need to deal with failures on these two:
        elif data["message"] == "FAILED":
            logger.error(f"Polar get_url failed: {data['message']}")
        else:
            logger.error("Polar get_url failed wrong serial number.")
        # logger.info(f"\nPolar S3 info \n{data['fields']}\n\n")

    async def _get_url(self, idle_or_print="printing"):
        """
        Request the image upload url from Polar Cloud. `idle_or_print` _must_
        be either "printing" or "idle".
        """
        logger.info(f"Polar _get_url {idle_or_print}")
        if idle_or_print not in ["idle", "printing"]:
            logger.error("Neither idle not printing was specified in getUrl request.")
        request_data = {
            "serialNumber": self.daemon.settings.get("polar.sn"),
            "method": "post",
            "type": idle_or_print,
            "jobId": self.job_id,
        }
        await self.socket.emit("getUrl", request_data)

    def _printer_action(self, which_action, print_file="") -> None:
        """
        Make dbus call to print, pause, cancel, resume. If printing a 3mf file
        we need the print_file name and the plate to print.
        """
        logger.info(f"Polar _printer_action {which_action} {print_file}")
        logger.info(
            '\n\n***\nPolar dbus json string: string: '
            f'\'{{"filePath": "{print_file}", "action": "{which_action}"}}\'\n**\n\n'
        )
        if print_file.endswith("3mf"):
            dbus_call = [
                "dbus-send",
                "--system",
                "--print-reply",
                "--dest=bbl.service.screen",
                "/bbl/service/screen",
                "bbl.screen.x1plus.PolarPrint3mf",
                f'string: {{"filePath": "{print_file}"}}',

            ]
            done = subprocess.run(dbus_call, capture_output=True).stdout.strip()
        else:
            dbus_call = [
                "dbus-send",
                "--system",
                "--print-reply",
                "--dest=bbl.service.screen",
                "/bbl/service/screen",
                "bbl.screen.x1plus.polarPrintGcode",
                f'string: {{"filePath": "{print_file}", "action": "{which_action}"}}',
            ]
            done = subprocess.run(dbus_call, capture_output=True).stdout.strip()
        logger.debug(done)

    def _set_interface(self) -> None:
        """
        Get IP and MAC addresses and store them in self.settings. This is
        intentionally dynamic as a security measure.
        """
        logger.info("Polar _set_interface")
        self.mac = get_MAC()
        self.ip = get_IP()

    def _serial_number(self) -> str:
        """
        Return the Bambu serial number—NOT the Polar Cloud SN. If emulating,
        random string.
        """
        logger.info("Polar _serial_number")
        if is_emulating:
            return "123456789"
        else:
            return utils_sn()

    def capture_frames(self, device_path="/dev/video20", output_dir="ipcam", num_frames=10, interval=2):
        """
        Capture num_frames single frame images from camera at interval of `interval`
        seconds and save to /sdcard/ipcam directory.

        To capture from nozzle cam, set `device_path` to /dev/video13.

        Note that many franes may be captured, that they will be overwritten on
        the next call to capture_frames, and that at no point are the captured
        frames deleted from the SD card, so use with caution.
        """
        logger.info("Polar capture_frames")
        try:
            os.makedirs(output_dir, exist_ok=True)
            pixel_format = "--set-fmt-video=width=1280,height=720,pixelformat=MJPG"
            if device_path == "/dev/video13":
                # Note I defaulted to YUYV here. Could also be NV12.
                pixel_format = "--set-fmt-video=width=640,height=480,pixelformat=YUYV"

            # cap_cmd = ["v4l2-ctl", "--device", device_path, "--all"]
            # result = subprocess.run(cap_cmd, capture_output=True, text=True)
            # logger.info(f"{result.stdout}")

            logger.debug("Setting video format.")
            fmt_cmd = [
                "v4l2-ctl",
                "--device",
                device_path,
                pixel_format,
            ]
            subprocess.run(fmt_cmd, check=True)

            for i in range(num_frames):
                output_file = os.path.join("/sdcard", output_dir, f"frame_{i}.jpg")
                capture_cmd = [
                    "v4l2-ctl",
                    "--device",
                    device_path,
                    "--stream-mmap=3",
                    "--stream-count=1",
                    "--stream-to",
                    output_file,
                ]

                result = subprocess.run(capture_cmd, capture_output=True, text=True)

                if result.returncode == 0:
                    logger.info(f"Saved frame_{i}.jpg")
                else:
                    logger.error(f"Failed to capture frame {i}. Error: {result.stderr}")

                if i < num_frames - 1:
                    time.sleep(interval)

        except subprocess.CalledProcessError as e:
            logger.error(f"An error occurred during capture: {str(e)}")
        except Exception as e:
            logger.error(f"unexpected error occurred: {str(e)}", exc_info=True)
