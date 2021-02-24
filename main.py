
import ssl
import asyncio
import websockets
import json
from concurrent.futures import TimeoutError
import random

import gi
gi.require_version('GLib', '2.0')
gi.require_version('GObject', '2.0')
gi.require_version('Gst', '1.0')
from gi.repository import Gst
gi.require_version('GstWebRTC', '1.0')
from gi.repository import GstWebRTC
gi.require_version('GstSdp', '1.0')
from gi.repository import GstSdp

PIPELINE_DESC = '''
webrtcbin name=sendrecv bundle-policy=max-bundle
 autovideosrc ! videoconvert ! queue ! vp8enc deadline=1 ! rtpvp8pay !
 queue ! application/x-rtp,media=video,encoding-name=VP8,payload=97 ! sendrecv.
'''

# ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
# ssl_context.check_hostname = False
# ssl_context.verify_mode = ssl.CERT_NONE

random.seed(123)

class JanusClient:
    def __init__(self, uri: str = ""):
        self.uri = uri
        self.received_transactions = dict()
        self.message_received_notifier = asyncio.Condition()
        self.ws = None

    async def connect(self) -> None:
        print("Connecting to: ", self.uri)
        # self.ws = await websockets.connect(self.uri, ssl=ssl_context)
        self.ws = await websockets.connect(self.uri, subprotocols=["janus-protocol"])
        self.receive_message_task = asyncio.create_task(self.receive_message())
        print("Connected")

    async def disconnect(self):
        print("Disconnecting")
        self.receive_message_task.cancel()
        await self.ws.close()

    async def receive_message(self):
        assert self.ws
        async for response_raw in self.ws:
            response = json.loads(response_raw)
            if "transaction" in response:
                async with self.message_received_notifier:
                    self.received_transactions[response["transaction"]] = response
                    self.message_received_notifier.notify_all()
            else:
                self.emit_event(response)

    async def send(self, message: dict) -> dict():
        transaction_id = str(random.randint(0, 9999))
        message["transaction"] = transaction_id
        print(json.dumps(message))
        await self.ws.send(json.dumps(message))
        while True:
            try:
                response = await asyncio.wait_for(self.get_transaction_reply(transaction_id), 5)
                print(response)
                return response
            except TimeoutError as e:
                print(e)
                print("Receive timeout")
                break

    async def get_transaction_reply(self, transaction_id):
        async with self.message_received_notifier:
            await self.message_received_notifier.wait_for(lambda: transaction_id in self.received_transactions)
            return self.received_transactions.pop(transaction_id)

    def emit_event(self, event_response: dict):
        print(event_response)

class WebRTCSubscriber:
    def __init__(self, client, session_id, handle_id):
        # self.id_ = id_
        self.pipe = None
        self.webrtc = None
        # self.peer_id = peer_id
        self.client = client
        self.session_id = session_id
        self.handle_id = handle_id
        self.started = False

    async def subscribe(self, feed_id):
        await self.client.send({
            "janus": "message",
            "session_id": self.session_id,
            "handle_id": self.handle_id,
            "body": {
                "request": "join",
                "ptype" : "subscriber",
                "room": 1234,
                "feed": feed_id,
                # "close_pc": True,
                # "audio": True,
                # "video": True,
                # "data": True,
                # "offer_audio": True,
                # "offer_video": True,
                # "offer_data": True,
                # "ack": True,
            }
        })

    async def unsubscribe(self):
        await self.client.send({
            "janus": "message",
            "session_id": self.session_id,
            "handle_id": self.handle_id,
            "body": {
                "request": "leave",
            }
        })
        self.pipe.set_state(Gst.State.NULL)

    async def send_start(self, jsep):
        await self.client.send({
            "janus": "message",
            "session_id": self.session_id,
            "handle_id": self.handle_id,
            "body": {
                "request": "start",
            },
            "jsep": jsep
        })

    def send_sdp_offer(self, offer):
        text = offer.sdp.as_text()
        print ('Sending offer:\n%s' % text)
        # msg = json.dumps({'sdp': {'type': 'offer', 'sdp': text}})
        loop = asyncio.get_event_loop()
        loop.run_until_complete(self.send_start({'type': 'offer', 'sdp': text}))

    def on_offer_created(self, promise, _, __):
        promise.wait()
        reply = promise.get_reply()
        offer = reply.get_value('offer')
        promise = Gst.Promise.new()
        self.webrtc.emit('set-local-description', offer, promise)
        promise.interrupt()
        self.send_sdp_offer(offer)

    def on_negotiation_needed(self, element):
        promise = Gst.Promise.new_with_change_func(self.on_offer_created, element, None)
        element.emit('create-offer', None, promise)

    async def send_ice_candidate_client(self, candidate):
        await self.client.send({
            "janus": "trickle",
            "session_id": self.session_id,
            "handle_id": self.handle_id,
            "candidate": candidate,
        })

    def send_ice_candidate_message(self, _, mlineindex, candidate):
        # icemsg = json.dumps({'candidate': {'candidate': candidate, 'sdpMLineIndex': mlineindex}})
        print({'candidate': candidate, 'sdpMLineIndex': mlineindex})
        loop = asyncio.get_event_loop()
        loop.run_until_complete(self.send_ice_candidate_client({'candidate': candidate, 'sdpMLineIndex': mlineindex}))

    def on_incoming_decodebin_stream(self, _, pad):
        if not pad.has_current_caps():
            print (pad, 'has no caps, ignoring')
            return

        caps = pad.get_current_caps()
        name = caps.to_string()
        if name.startswith('video'):
            q = Gst.ElementFactory.make('queue')
            conv = Gst.ElementFactory.make('videoconvert')
            sink = Gst.ElementFactory.make('autovideosink')
            self.pipe.add(q)
            self.pipe.add(conv)
            self.pipe.add(sink)
            self.pipe.sync_children_states()
            pad.link(q.get_static_pad('sink'))
            q.link(conv)
            conv.link(sink)
        elif name.startswith('audio'):
            q = Gst.ElementFactory.make('queue')
            conv = Gst.ElementFactory.make('audioconvert')
            resample = Gst.ElementFactory.make('audioresample')
            sink = Gst.ElementFactory.make('autoaudiosink')
            self.pipe.add(q)
            self.pipe.add(conv)
            self.pipe.add(resample)
            self.pipe.add(sink)
            self.pipe.sync_children_states()
            pad.link(q.get_static_pad('sink'))
            q.link(conv)
            conv.link(resample)
            resample.link(sink)

    def on_incoming_stream(self, _, pad):
        if pad.direction != Gst.PadDirection.SRC:
            return

        decodebin = Gst.ElementFactory.make('decodebin')
        decodebin.connect('pad-added', self.on_incoming_decodebin_stream)
        self.pipe.add(decodebin)
        decodebin.sync_state_with_parent()
        self.webrtc.link(decodebin)

    def start_pipeline(self):
        self.pipe = Gst.parse_launch(PIPELINE_DESC)
        self.webrtc = self.pipe.get_by_name('sendrecv')
        self.webrtc.connect('on-negotiation-needed', self.on_negotiation_needed)
        self.webrtc.connect('on-ice-candidate', self.send_ice_candidate_message)
        self.webrtc.connect('pad-added', self.on_incoming_stream)
        self.pipe.set_state(Gst.State.PLAYING)

    async def handle_sdp(self, message):
        assert (self.webrtc)
        msg = json.loads(message)
        if 'sdp' in msg:
            sdp = msg['sdp']
            assert(sdp['type'] == 'answer')
            sdp = sdp['sdp']
            print ('Received answer:\n%s' % sdp)
            res, sdpmsg = GstSdp.SDPMessage.new()
            GstSdp.sdp_message_parse_buffer(bytes(sdp.encode()), sdpmsg)
            answer = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.ANSWER, sdpmsg)
            promise = Gst.Promise.new()
            self.webrtc.emit('set-remote-description', answer, promise)
            promise.interrupt()
        elif 'ice' in msg:
            ice = msg['ice']
            candidate = ice['candidate']
            sdpmlineindex = ice['sdpMLineIndex']
            self.webrtc.emit('add-ice-candidate', sdpmlineindex, candidate)

    async def loop(self):
        assert self.conn
        async for message in self.conn:
            if message == 'HELLO':
                await self.setup_call()
            elif message == 'SESSION_OK':
                self.start_pipeline()
            elif message.startswith('ERROR'):
                print (message)
                return 1
            else:
                await self.handle_sdp(message)
        return 0

async def subscribe_feed(client, session_id, handle_id):
    # response_list_participants = await client.send({
    #     "janus": "message",
    #     "session_id": session_id,
    #     "handle_id": handle_id,
    #     "body": {
    #         "request": "listparticipants",
    #         "room": 1234,
    #     }
    # })
    # if len(response_list_participants["plugindata"]["data"]["participants"]) > 0:
    #     # Publishers available
    #     participants_data_1 = response_list_participants["plugindata"]["data"]["participants"][0]
    #     # print(publisher_data)
    #     participant_id = participants_data_1["id"]
    #     subscriber_client = WebRTCSubscriber(client, session_id, handle_id)
    #     await subscriber_client.subscribe(participant_id)
    #     # await client.send({
    #     #     "janus": "message",
    #     #     "session_id": session_id,
    #     #     "handle_id": handle_id,
    #     #     "body": {
    #     #         "request": "start",
    #     #     }
    #     # })
    #     subscriber_client.start_pipeline()
    #     await asyncio.sleep(5)
    #     await subscriber_client.unsubscribe()
    response_publish = await client.send({
        "janus": "message",
        "session_id": session_id,
        "handle_id": handle_id,
        "body": {
            "request": "join",
            "ptype" : "publisher",
            "room": 1234,
            "id": 333,
            "display": "qweasd"
        }
    })
    if len(response_publish["plugindata"]["data"]["publishers"]) > 0:
        # Publishers available
        publishers_data_1 = response_publish["plugindata"]["data"]["publishers"][0]
        # print(publisher_data)
        publisher_id = publishers_data_1["id"]
        # Attach subscriber plugin
        response_plugin_subscriber = await client.send({
            "janus": "attach",
            "session_id": session_id,
            "plugin": "janus.plugin.videoroom",
        })
        if response_plugin_subscriber["janus"] == "success":
            # Plugin attached
            subscriber_client = WebRTCSubscriber(client, session_id, response_plugin_subscriber["data"]["id"])
            response_publish = await client.send({
                "janus": "message",
                "session_id": session_id,
                "handle_id": response_plugin_subscriber["data"]["id"],
                "body": {
                    "request": "join",
                    "ptype" : "subscriber",
                    "room": 1234,
                    "feed": publisher_id,
                    "close_pc": True,
                    "audio": True,
                    "video": True,
                    "data": True,
                    "offer_audio": True,
                    "offer_video": True,
                    "offer_data": True,
                    "ack": False,
                }
            })
            # await subscriber_client.subscribe(publisher_id)
            # subscriber_client.start_pipeline()
            await asyncio.sleep(10)
            # await subscriber_client.unsubscribe()
            # Destroy subscriber plugin
            response_leave = await client.send({
                "janus": "message",
                "session_id": session_id,
                "handle_id": response_plugin_subscriber["data"]["id"],
                "body": {
                    "request": "leave",
                }
            })
            response_detach = await client.send({
                "janus": "detach",
                "session_id": session_id,
                "handle_id": response_plugin_subscriber["data"]["id"],
            })
        # await client.send({
        #     "janus": "message",
        #     "session_id": session_id,
        #     "handle_id": handle_id,
        #     "body": {
        #         "request": "start",
        #     }
        # })
    response_leave = await client.send({
        "janus": "message",
        "session_id": session_id,
        "handle_id": handle_id,
        "body": {
            "request": "leave",
        }
    })

async def create_plugin(client, session_id):
    # Attach plugin
    response_plugin = await client.send({
        "janus": "attach",
        "session_id": session_id,
        "plugin": "janus.plugin.videoroom",
    })
    if response_plugin["janus"] == "success":
        # Plugin attached
        await subscribe_feed(client, session_id, response_plugin["data"]["id"])
        # Destroy plugin
        response_detach = await client.send({
            "janus": "detach",
            "session_id": session_id,
            "handle_id": response_plugin["data"]["id"],
        })

async def main():
    client = JanusClient("ws://lt.limmengkiat.name.my/janusws/")
    await client.connect()
    # Create session
    response = await client.send({
        "janus": "create",
    })
    if response["janus"] == "success":
        # Session created
        # # Attach plugin
        # response_plugin = await client.send({
        #     "janus": "attach",
        #     "session_id": response["data"]["id"],
        #     "plugin": "janus.plugin.echotest",
        # })
        # print(response_plugin)
        # if response_plugin["janus"] == "success":
        #     # Plugin attached
        #     # Destroy plugin
        #     response_detach = await client.send({
        #         "janus": "detach",
        #         "session_id": response["data"]["id"],
        #         "handle_id": response_plugin["data"]["id"],
        #     })
        #     print(response_detach)
        # await asyncio.gather(create_plugin(client, response["data"]["id"]), create_plugin(client, response["data"]["id"]))
        await create_plugin(client, response["data"]["id"])
        # Destroy session
        reponse_destroy = await client.send({
            "janus": "destroy",
            "session_id": response["data"]["id"],
        })
    await client.disconnect()
    print("End of main")

def check_plugins():
    needed = ["opus", "vpx", "nice", "webrtc", "dtls", "srtp", "rtp",
              "rtpmanager", "videotestsrc", "audiotestsrc"]
    missing = list(filter(lambda p: Gst.Registry.get().find_plugin(p) is None, needed))
    if len(missing):
        print('Missing gstreamer plugins:', missing)
        return False
    return True

Gst.init(None)
check_plugins()
asyncio.run(main())