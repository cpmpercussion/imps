import logging
import time
import datetime
import empi_mdrnn
import numpy as np
import queue
from pythonosc import dispatcher
from pythonosc import osc_server
from pythonosc import osc_message_builder
from pythonosc import udp_client
import argparse
from threading import Thread
import tensorflow as tf
from keras import backend as K


# Input and output to serial are bytes (0-255)
# Output to Pd is a float (0-1)
parser = argparse.ArgumentParser(description='Predictive Musical Interaction MDRNN Interface.')
parser.add_argument('-l', '--log', dest='logging', action="store_true", help='Save input and RNN data to a log file.')
parser.add_argument('-g', '--nogui', dest='nogui', action='store_true', help='Disable the TKinter GUI.')
# Individual Modes
parser.add_argument('-o', '--only', dest='useronly', action="store_true", help="User control only mode, no RNN.")
parser.add_argument('-r', '--rnn', dest='rnnonly', action="store_true", help='RNN interaction only.')
# Duo Modes
parser.add_argument('-c', '--call', dest='callresponse', action="store_true", help='Call and response mode.')
parser.add_argument('-p', '--polyphony', dest='polyphony', action="store_true", help='Harmony mode.')
parser.add_argument('-b', '--battle', dest='battle', action="store_true", help='Battle royale mode.')
# OSC addresses
parser.add_argument("--clientip", default="localhost", help="The address of output device.")
parser.add_argument("--clientport", type=int, default=5000, help="The port the output device is listening on.")
parser.add_argument("--serverip", default="localhost", help="The address of this server.")
parser.add_argument("--serverport", type=int, default=5001, help="The port this server should listen on.")
# MDRNN arguments.
parser.add_argument('-d', '--dimension', dest='dimension', default=2, help='The dimension of the data to model, must be >= 2.')
parser.add_argument("--modelsize", default="s", help="The model size: s, m, l, xl")
parser.add_argument("--sigmatemp", default=0.01, help="The sigma temperature for sampling.")
parser.add_argument("--pitemp", default=1, help="The pi temperature for sampling.")
args = parser.parse_args()

LOG_FILE = datetime.datetime.now().isoformat().replace(":", "-")[:19] + "-mdrnn.log"  # Log file name.
LOG_FORMAT = '%(message)s'
# ## OSC and Serial Communication
# Details for OSC output
INPUT_MESSAGE_ADDRESS = "/interface"
OUTPUT_MESSAGE_ADDRESS = "/prediction"

# TODO: set up OSC server
# TODO: set up OSC client
# TODO set up interface to build MDRNN
# TODO set up run loop for inference.

osc_client = udp_client.SimpleUDPClient(args.clientip, args.clientport)
# osc_client.send_message("OUTPUT_MESSAGE_ADDRESS", random.random())

# ## Load the Model
compute_graph = tf.Graph()
with compute_graph.as_default():
   sess = tf.Session()

# Choose model parameters.
if args.modelsize is 's':
    mdrnn_units = 64
    mdrnn_mixes = 5
    mdrnn_layers = 2
elif args.modelsize is 'm':
    mdrnn_units = 128
    mdrnn_mixes = 5
    mdrnn_layers = 2
elif args.modelsize is 'l':
    mdrnn_units = 256
    mdrnn_mixes = 5
    mdrnn_layers = 2
elif args.modelsize is 'xl':
    mdrnn_units = 512
    mdrnn_mixes = 5
    mdrnn_layers = 3
else:
    mdrnn_units = 128
    mdrnn_mixes = 5
    mdrnn_layers = 2


def build_network(sess):
    """Build the MDRNN."""
    # Hyperparameters
    empi_mdrnn.MODEL_DIR = "./models/"
    #model_file = "./models/empi_mdrnn-layers2-units128-mixtures5-scale10-E84-VL-3.68.hdf5"
    # Instantiate Running Network
    K.set_session(sess)
    with compute_graph.as_default():
        net = empi_mdrnn.PredictiveMusicMDRNN(mode=empi_mdrnn.NET_MODE_RUN,
                                              dimension=args.dimension,
                                              n_hidden_units=mdrnn_units,
                                              n_mixtures=mdrnn_mixes,
                                              layers=mdrnn_layers)
        net.pi_temp = args.pitemp
        net.sigma_temp = args.sigmatemp
    print("MDRNN Loaded.")
    return net

net = build_network(sess)
rnn_output_buffer = queue.Queue()
writing_queue = queue.Queue()
# Touch storage for RNN.
last_rnn_touch = empi_mdrnn.random_sample(out_dim=args.dimension)  # prepare previos sample.
last_user_touch = empi_mdrnn.random_sample(out_dim=args.dimension)
last_user_interaction = time.time()
CALL_RESPONSE_THRESHOLD = 2.0
call_response_mode = 'call'
# Interaction Loop Parameters
# All set to false before setting is chosen.
thread_running = False
# user_to_sound = False
user_to_rnn = False
rnn_to_rnn = False
rnn_to_sound = False
listening_as_well = False

if args.logging:
    logging.basicConfig(filename=LOG_FILE,
                        level=logging.INFO,
                        format=LOG_FORMAT)
    print("Logging enabled:", LOG_FILE)

# Interactive Mapping
if args.callresponse:
    print("Entering call and response mode.")
    # set initial conditions.
    # user_to_sound = True
    user_to_rnn = True
    rnn_to_rnn = False
    rnn_to_sound = False
elif args.polyphony:
    print("Entering polyphony mode.")
    # user_to_sound = True
    user_to_rnn = True
    rnn_to_rnn = False
    rnn_to_sound = True
elif args.battle:
    print("Entering battle royale mode.")
    # user_to_sound = True
    user_to_rnn = False
    rnn_to_rnn = True
    rnn_to_sound = True
elif args.useronly:
    print("Entering user only mode.")
    # user_to_sound = True
elif args.rnnonly:
    print("RNN Playback only mode.")
    # user_to_sound = False
    user_to_rnn = False
    rnn_to_rnn = True
    rnn_to_sound = True


def handle_interface_message(address: str, *osc_arguments) -> None:
    """Handler for OSC messages from the interface"""
    global last_user_touch
    global last_user_interaction
    global last_rnn_touch
    print(osc_arguments)
    userloc = None
    logging.info("{1},user,{0}".format(userloc, datetime.datetime.now().isoformat()))
    userdt = time.time() - last_user_interaction
    last_user_interaction = time.time()
    last_user_touch = np.array([userdt, userloc])
    # These values are accessed by the RNN in the interaction loop function.


def make_prediction(sess, compute_graph):
    # Interaction loop: reads input, makes predictions, outputs results.
    global last_user_touch
    global last_user_interaction
    global last_rnn_touch
    # Make predictions.
    # Need someway to know when a prediction is waiting to be made. use a queue as well.

    if user_to_rnn:
        K.set_session(sess)
        with compute_graph.as_default():
            rnn_output = net.generate_touch(last_user_touch)
        print("conditioned RNN state", str(time.time()))
        if rnn_to_sound:
            rnn_output_buffer.put_nowait(rnn_output)
    if rnn_to_rnn and rnn_output_buffer.empty():
        K.set_session(sess)
        with compute_graph.as_default():
            rnn_output = net.generate_touch(last_rnn_touch)
        print("made RNN prediction in:", last_rnn_touch, "out:", rnn_output)
        rnn_output_buffer.put_nowait(rnn_output)  # put it in the playback queue.


def playback_rnn_loop():
    # Plays back RNN notes from its buffer queue.
    global last_rnn_touch
    while True:
        item = rnn_output_buffer.get(block=True, timeout=None)  # Blocks until next item is available.
        print("processing an rnn command", time.time())
        # convert to dt, byte format
        dt = item[0]
        x_loc = min(max(item[1], 0), 1)  # x_loc in [0,1]
        dt = max(dt, 0.001)  # stop accidental minus and zero dt.
        time.sleep(dt)  # wait until time to play the sound
        last_rnn_touch = np.array([dt, x_loc])  # set the last rnn movement to the corrected value.
        if rnn_to_sound:
            # RNN can be disconnected from sound
            send_sound_command(touch_message_datagram(address='rnn', pos=x_loc))
            # TODO, send sound via OSC.
            print("RNN Played:", x_loc, "at", dt)
            logging.info("{1},rnn,{0}".format(x_loc, datetime.datetime.now().isoformat()))
        rnn_output_buffer.task_done()


def monitor_user_action():
    # Handles changing responsibility in Call-Response mode.
    global call_response_mode
    global user_to_rnn
    global rnn_to_rnn
    global rnn_to_sound
    # Check when the last user interaction was
    dt = time.time() - last_user_interaction
    if dt > CALL_RESPONSE_THRESHOLD:
        # switch to response modes.
        user_to_rnn = False
        rnn_to_rnn = True
        rnn_to_sound = True
        if call_response_mode is 'call':
            print("switching to response.")
            call_response_mode = 'response'
    else:
        # switch to call mode.
        user_to_rnn = True
        rnn_to_rnn = False
        rnn_to_sound = False
        if call_response_mode is 'response':
            print("switching to call.")
            call_response_mode = 'call'
            print("Clearning RNN buffer")
            while not rnn_output_buffer.empty():
                rnn_output_buffer.get()
                rnn_output_buffer.task_done()
                print("Cleared an RNN buffer item")
            print("ready for call mode")


# Set up OSC Server
disp = dispatcher.Dispatcher()
disp.map(INPUT_MESSAGE_ADDRESS, handle_interface_message)
server = osc_server.ThreadingOSCUDPServer((args.serverip, args.serverport), disp)
print("Serving on {}".format(server.server_address))


print("Now running...")
thread_running = True

# Set up run loop.
print("preparing MDRNN.")
K.set_session(sess)
with compute_graph.as_default():
    net.load_model() # try loading from default file location.
# condition = Condition()
# rnn_thread = Thread(target=playback_rnn_loop, name="rnn_player_thread", args=(condition,), daemon=True)
print("preparting MDRNN thread.")
rnn_thread = Thread(target=playback_rnn_loop, name="rnn_player_thread", daemon=True)
print("Preparing Server thread.")
server_thread = Thread(target=server.serve_forever, name="server_thread", daemon=True)
print("starting up.")
try:
    rnn_thread.start()
    server_thread.start()
    while True:
        make_prediction(sess, compute_graph)
        if args.callresponse:
            monitor_user_action()
except KeyboardInterrupt:
    print("\nCtrl-C received... exiting.")
    thread_running = False
    rnn_thread.join(timeout=1)
    server_thread.join(timeout=1)
    pass
finally:
    print("\nDone, shutting down.")